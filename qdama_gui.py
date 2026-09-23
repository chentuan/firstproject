#!/usr/bin/env python3
"""钱大妈智慧中台「日常订购」—— 图形界面。

把 ``qdama_daily.py`` 那套接口调用搬到窗口里：输账号密码登录、选门店和到店日期、
按查询条件筛、结果按页表格展示。业务逻辑（签名、请求形状、筛选、分页、合计）
一行不改，全在 ``qdama_daily`` 里；这里只负责"摆到屏幕上"。

界面是照着网页版「日常订购」页复刻的：同样的查询条件、同样的表格列（列名、
顺序、宽度都抄自页面打包产物）、同样的"合计品项 / 订购数量"、同样的每页条数。
复刻的是**结构与文案**，不是像素——Element UI 的圆角阴影跟着来没有意义。

**线程模型**：所有网络请求都跑在后台线程，事件经 ``queue`` 交回主线程。
tkinter 不是线程安全的——从别的线程碰控件不会立刻报错，而是在某个随机的时刻
崩掉。所以后台线程只允许做一件事：``queue.put``。

**查询不发网络请求。** 页面上那些查询条件本来就是前端本地过滤的（接口只收
日期 + 门店），这里照做：拉一天的数据，然后在本地筛。好处是点「查询」是瞬间
响应；代价是看到的是"拉取那一刻"的快照，要最新的就再点一次「刷新数据」。

依赖只有 tkinter（标准库）。界面背后的取数、筛选逻辑都能脱离窗口单独调用，
``--auto`` 自检走的就是那条路。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import qdama_daily as q

APP_TITLE = "钱大妈 · 日常订购"
# 界面偏好（门店码、每页条数、上次用的账号）。**不存密码**。
CONFIG_PATH = "~/.qdama_gui.json"
OUTPUT_DIR = "~/Downloads"

# 事件类型：后台线程 → 主线程
EV_PROGRESS = "progress"
EV_ERROR = "error"
EV_ROWS = "rows"
EV_LOGIN_OK = "login-ok"
EV_SAVED_TOKEN_OK = "saved-token-ok"

# 「全部」是每个下拉的第一项，选中它等于不筛。
ALL = "全部"
PAGE_SIZES = (10, 20, 50, 100)

# --------------------------------------------------------------------------- #
# 配色：整界面固定浅色，**不跟随 macOS 深色模式**
#
# 为什么不让它跟随：tkinter 的 aqua 主题把 Treeview 的配色指向系统色
# （`systemTextBackgroundColor` / `systemTextColor`）。系统处于深色模式时，
# 文字色解析成**白色**；而"隔行浅色"只设了背景没设前景 → 白字压在浅灰底上，
# 整行内容糊成一片。表格是拿来读数字的，这种地方不能交给系统主题去猜。
#
# 所以这里显式指定每个颜色，并把主题切到 clam —— clam 会老老实实按给定的
# 十六进制值渲染，aqua 在 macOS 上对部分控件属性是忽略的。
# --------------------------------------------------------------------------- #

BG = "#eef1f5"          # 窗口底
CARD = "#ffffff"        # 卡片 / 表格底
TEXT = "#1f2328"        # 主文字
MUTED = "#6b7280"       # 次要文字
BORDER = "#d5dbe3"      # 描边
BRAND = "#d81e06"       # 钱大妈红
BRAND_DARK = "#b3160a"  # 悬停 / 按下
HEAD_BG = "#e9edf3"     # 表头
ROW_ODD = "#f6f8fb"     # 隔行
ROW_EVEN = "#ffffff"
SELECT_BG = "#fbe0dc"   # 选中行（红系淡底）
DANGER = "#c0392b"      # 报错文字


# --------------------------------------------------------------------------- #
# 不依赖窗口的部分：筛选链
#
# 单独拎出来是为了能离线断言。窗口只是调用方，不参与计算。
# --------------------------------------------------------------------------- #

def apply_filters(rows: list[dict], params: dict, status: str) -> list[dict]:
    """状态切换 → 查询条件。顺序与页面一致（先按状态，再按条件）。"""
    return q.filter_rows(q.filter_by_status(rows, status), params)


def category_choices(rows: list[dict], big: str = "", mid: str = "") -> dict[str, list[str]]:
    """三级分类的候选值，让下级随上级收窄（页面是级联选择器）。

    页面的选项来自分类树接口，这里改成从**已加载的数据**里现取：不用多打一个
    接口，也不会因为分类树里挂着一堆当天没货的分类而选出空结果。
    """
    def uniq(values) -> list[str]:
        return sorted({value for value in values if value})

    scoped = rows
    if big:
        scoped = [row for row in scoped if str(row.get("bigcategoryname") or "") == big]

    mid_scoped = scoped
    if mid:
        mid_scoped = [row for row in mid_scoped
                      if str(row.get("midcategoryname") or "") == mid]

    return {
        "bigcategoryname": uniq(str(row.get("bigcategoryname") or "") for row in rows),
        "midcategoryname": uniq(str(row.get("midcategoryname") or "") for row in scoped),
        "subcategoryname": uniq(str(row.get("subcategoryname") or "") for row in mid_scoped),
    }


def cell_text(row: dict, field: str) -> str:
    """把一行的某列转成显示文本。

    「组合套餐」页面用的是 ``1 == comboflag ? "是" : ""``——不是 1 就显示空白，
    不是显示 0。这一条照抄，别自作聪明改成"是/否"。
    """
    if field == "comboflag":
        value = row.get("comboflag")
        return "是" if str(value) == "1" else ""
    value = row.get(field)
    return "" if value is None else str(value)


def rows_to_csv(path: str, rows: list[dict]) -> int:
    """把（已筛选的）行写成 CSV。列取页面那 13 列，表头用中文。

    写 utf-8-sig：不加 BOM 的话 Excel 打开中文是乱码。
    """
    fields = [field for field, _label, _width in q.PAGE_COLUMNS]
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _field, label, _width in q.PAGE_COLUMNS])
        for row in rows:
            writer.writerow([cell_text(row, field) for field in fields])
    return len(rows)


# --------------------------------------------------------------------------- #
# 窗口
# --------------------------------------------------------------------------- #

class QdamaApp:
    def __init__(self, root: tk.Misc, *, fetcher=None, login_fn=None,
                 autostart: bool = True, config_path: str = CONFIG_PATH) -> None:
        self.root = root
        # 依赖注入：自检时换成假实现，就不用真打网络了
        self._fetcher = fetcher or q.fetch_daily_page
        self._login_fn = login_fn or q.login
        # autostart=False 时不做启动取数。自检必须这样——否则延迟回调会在断言
        # 中途拿真实数据把假数据顶掉，测出来的是网络状态而不是界面逻辑。
        self._autostart = autostart

        self._config_path = os.path.expanduser(config_path)
        self._config = self._load_config()
        self._queue: queue.Queue = queue.Queue()
        self._busy = False

        self.token = ""
        self.token_source = "(未提供)"
        self.session: dict = q.load_session()

        self.all_rows: list[dict] = []      # 这次拉回来的全量（一天）
        self.view_rows: list[dict] = []     # 状态 + 条件筛完的
        self.page = 1
        self.size = int(self._config.get("page_size") or PAGE_SIZES[0])
        self.server_summary = {"totalskuqty": 0, "totalorderqty": "0.00"}

        self.filter_vars: dict[str, tk.StringVar] = {}
        self.filter_combos: dict[str, ttk.Combobox] = {}
        self._sales_display_to_code: dict[str, str] = {}
        self._context_display_to_id: dict[str, str] = {}

        self._build()
        self._load_session_shops()
        self._pump()

    # ------------------------------------------------------------ 配置读写

    def _load_config(self) -> dict:
        try:
            with open(self._config_path, encoding="utf-8") as handle:
                data = json.load(handle)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_config(self) -> None:
        self._config["page_size"] = self.size
        self._config["sapshopid"] = self.store_var.get().strip()
        remember = bool(getattr(self, "remember_var", None) and self.remember_var.get())
        self._config["remember"] = remember
        # 不勾"记住账号"就连账号也不留；密码任何时候都不落盘
        self._config["last_user"] = self.user_var.get().strip() if remember else ""
        try:
            with open(self._config_path, "w", encoding="utf-8") as handle:
                json.dump(self._config, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass                            # 存不下就算了，不值得弹窗打扰

    # ---------------------------------------------------------------- 搭界面

    def _build(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("1360x820")
        self.root.minsize(1000, 600)
        self._setup_style()

        self.container = ttk.Frame(self.root, padding=10)
        self.container.pack(fill="both", expand=True)

        self.login_frame = self._build_login(self.container)
        self.main_frame = self._build_main(self.container)
        # **先登录、后列表。** 不在这里偷偷用本地 token 进主界面——
        # 必须账号密码换到新 token 并且落盘成功，才切过去（见 _on_login_ok）。
        self._show_login()
        if self._autostart:
            self.root.after(150, self._prepare_login)

    def _setup_style(self) -> None:
        """把 ttk 的每个控件都钉死成浅色，别继承系统外观。"""
        style = ttk.Style()
        # clam 会按给定色值渲染；aqua 在 macOS 上会忽略 Treeview 的部分属性
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.root.configure(bg=BG)
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=CARD)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        # 卡片（白底）里的标签、勾选框必须跟着白底走，否则会在白卡片上留一块灰
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Card.TCheckbutton", background=CARD, foreground=TEXT,
                        focuscolor=CARD)
        style.map("Card.TCheckbutton", background=[("active", CARD)])
        style.configure("Small.TButton", background="#e7ebf0", foreground="#2b6cb0",
                        bordercolor=BORDER, focuscolor=CARD, padding=(8, 3),
                        font=("", 10))
        style.map("Small.TButton",
                  background=[("active", "#dbe0e7"), ("disabled", "#f0f2f5")],
                  foreground=[("disabled", "#9aa2ad")])
        style.configure("TButton", background="#e3e7ed", foreground=TEXT,
                        bordercolor=BORDER, focuscolor=BG, padding=(10, 4))
        style.map("TButton",
                  background=[("active", "#d7dce4"), ("disabled", "#eef1f5")],
                  foreground=[("disabled", "#9aa2ad")])
        # 登录按钮：红底白字，跟网页版的主色一致
        style.configure("Brand.TButton", background=BRAND, foreground="#ffffff",
                        bordercolor=BRAND_DARK, focuscolor=BRAND, padding=(10, 7),
                        font=("", 13, "bold"))
        style.map("Brand.TButton",
                  background=[("active", BRAND_DARK), ("disabled", "#e5a9a2")],
                  foreground=[("disabled", "#fdf1f0")])
        style.configure("TEntry", fieldbackground=CARD, foreground=TEXT,
                        bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                        insertcolor=TEXT, padding=4)
        style.configure("TCombobox", fieldbackground=CARD, foreground=TEXT,
                        bordercolor=BORDER, arrowcolor=TEXT, padding=3)
        style.map("TCombobox",
                  fieldbackground=[("readonly", CARD), ("disabled", BG)],
                  foreground=[("disabled", MUTED)])
        style.configure("TCheckbutton", background=CARD, foreground=TEXT, focuscolor=CARD)
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("TRadiobutton", background=BG, foreground=TEXT, focuscolor=BG)
        style.map("TRadiobutton", background=[("active", BG)])
        style.configure("Horizontal.TProgressbar", background=BRAND, troughcolor="#dfe4ea",
                        bordercolor="#dfe4ea", lightcolor=BRAND, darkcolor=BRAND)

        # 表格：**背景和前景都显式给**，这样不管系统是深色还是浅色都能读。
        style.configure("Qdama.Treeview",
                        background=CARD, fieldbackground=CARD, foreground=TEXT,
                        bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                        rowheight=27, font=("", 12))
        style.map("Qdama.Treeview",
                  background=[("selected", SELECT_BG)],
                  foreground=[("selected", TEXT)])
        style.configure("Qdama.Treeview.Heading",
                        background=HEAD_BG, foreground=TEXT,
                        bordercolor=BORDER, relief="flat", font=("", 12, "bold"),
                        padding=(2, 6))
        style.map("Qdama.Treeview.Heading",
                  background=[("active", "#dfe4ea")],
                  foreground=[("active", TEXT)])
        # 滚动条也给个中性色，默认那套在浅底上很突兀
        for orient in ("Vertical", "Horizontal"):
            style.configure(f"{orient}.TScrollbar",
                            background="#d9dee6", troughcolor=BG,
                            bordercolor=BG, arrowcolor=TEXT)

    # 登录页 --------------------------------------------------------------

    def _build_login(self, parent: tk.Misc) -> ttk.Frame:
        """登录页：照网页版登录页的样子做——白卡片 + 品牌红头 + 账号密码。

        这里是**唯一入口**：不登录拿不到 token，就拿不到列表页。
        """
        frame = ttk.Frame(parent)
        # 用 tk.Frame 而不是 ttk.Frame 是为了能画描边（ttk 在 macOS 上对
        # border 的处理不听话，画出来的卡片会缺边）
        card = tk.Frame(frame, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1, bd=0)
        card.place(relx=0.5, rely=0.5, anchor="center")
        card.columnconfigure(0, weight=1)

        # 品牌头
        banner = tk.Frame(card, bg=BRAND)
        banner.grid(row=0, column=0, sticky="ew")
        tk.Label(banner, text="钱 大 妈", bg=BRAND, fg="#ffffff",
                 font=("", 22, "bold")).pack(pady=(16, 0))
        tk.Label(banner, text="智慧运营平台 · 日常订购", bg=BRAND, fg="#ffe3df",
                 font=("", 11)).pack(pady=(2, 16))

        body = tk.Frame(card, bg=CARD)
        body.grid(row=1, column=0, sticky="nsew", padx=34, pady=(24, 10))
        body.columnconfigure(1, weight=1)

        self.user_var = tk.StringVar(value=str(self._config.get("last_user") or ""))
        self.pass_var = tk.StringVar()
        self.remember_var = tk.BooleanVar(value=bool(self._config.get("remember", True)))

        ttk.Label(body, text="账号", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        user_entry = ttk.Entry(body, textvariable=self.user_var, width=28)
        user_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 14))

        ttk.Label(body, text="密码", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=(0, 6))
        pass_entry = ttk.Entry(body, textvariable=self.pass_var, width=28, show="•")
        pass_entry.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        # 密码框里敲回车直接登录，省得去够按钮
        pass_entry.bind("<Return>", lambda _event: self.on_login())

        ttk.Checkbutton(body, text="记住账号（只存账号，不存密码）",
                        variable=self.remember_var,
                        style="Card.TCheckbutton").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 14))

        self.login_button = ttk.Button(body, text="登 录", command=self.on_login,
                                       style="Brand.TButton")
        self.login_button.grid(row=5, column=0, columnspan=2, sticky="ew")

        self.login_msg_var = tk.StringVar()
        ttk.Label(body, textvariable=self.login_msg_var, style="Card.TLabel",
                  foreground=DANGER, wraplength=300, justify="center").grid(
            row=6, column=0, columnspan=2, pady=(12, 0))

        # 逃生口：本机已经有 token 文件时，允许跳过登录（省得每次重敲密码）。
        # 它会先验一次 token 再放行，不是无脑进主界面。
        self.token_button = ttk.Button(
            body, text="用本地已保存的 token 进入",
            command=self.on_use_saved_token, style="Small.TButton")
        self.token_button.grid(row=7, column=0, columnspan=2, pady=(10, 0))
        self.token_button.grid_remove()         # 有 token 才显示，_prepare_login 决定

        ttk.Label(card,
                  text="密码只在内存里用一次，RSA 加密后立即发出，不落盘；\n"
                       "登录成功后 token 写入 ~/Downloads/qdama-token.txt（权限 600）。",
                  style="Card.TLabel", foreground=MUTED, justify="center",
                  font=("", 10)).grid(row=2, column=0, padx=30, pady=(0, 18))

        self.login_user_entry = user_entry
        self.login_pass_entry = pass_entry
        return frame

    # 主界面 --------------------------------------------------------------

    def _build_main(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)          # 表格那行吃掉多余高度

        self._build_toolbar(frame).grid(row=0, column=0, sticky="ew")
        self._build_filters(frame).grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self._build_table(frame).grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self._build_footer(frame).grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self._build_status(frame).grid(row=4, column=0, sticky="ew", pady=(6, 0))
        return frame

    def _build_toolbar(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.Frame(parent)

        # 门店拆成两个，因为它俩真的不是一回事：
        #   查询门店（sapshopid）→ 要查哪个门店的数据，可以是任意门店；
        #   权限门店（X-QDM-Shop-Id）→ 以哪个门店的身份查，**必须是账号有权限的**
        #   门店，否则服务端回 100006（"没有 scn:purchase:view 权限"），
        #   看着像功能没开，其实是门店填错了。
        ttk.Label(box, text="查询门店").pack(side="left")
        self.store_var = tk.StringVar(value=str(self._config.get("sapshopid") or ""))
        self.store_combo = ttk.Combobox(box, textvariable=self.store_var, width=9)
        self.store_combo.pack(side="left", padx=(6, 12))
        self.store_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_store_hint())

        ttk.Label(box, text="权限门店").pack(side="left")
        self.context_var = tk.StringVar()
        self.context_combo = ttk.Combobox(box, textvariable=self.context_var, width=18)
        self.context_combo.pack(side="left", padx=(6, 16))
        self.context_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_store_hint())

        ttk.Label(box, text="到店日期").pack(side="left")
        self.date_var = tk.StringVar(value=dt.date.today().strftime("%Y-%m-%d"))
        date_entry = ttk.Entry(box, textvariable=self.date_var, width=12)
        date_entry.pack(side="left", padx=(6, 4))
        # 改完日期顺手把数据也换了——页面也是这个行为（watch queryStr）
        date_entry.bind("<Return>", lambda _event: self.on_refresh())

        ttk.Button(box, text="今天", width=6,
                   command=lambda: self._set_date(0)).pack(side="left")
        ttk.Button(box, text="昨天", width=6,
                   command=lambda: self._set_date(1)).pack(side="left", padx=(4, 0))

        self.refresh_button = ttk.Button(box, text="刷新数据", command=self.on_refresh)
        self.refresh_button.pack(side="left", padx=(16, 0))

        self.logout_button = ttk.Button(box, text="退出登录", command=self.on_logout)
        self.logout_button.pack(side="right")
        self.who_var = tk.StringVar(value="未登录")
        ttk.Label(box, textvariable=self.who_var, foreground=MUTED).pack(
            side="right", padx=(0, 10)
        )
        return box

    def _build_filters(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.LabelFrame(parent, text="查询条件", padding=10)
        for column in range(3):
            box.columnconfigure(column * 2 + 1, weight=1)

        # 第 1 行：三个下拉（选项抄自页面 el-option）
        # 第 2 行：编码 / 名称是输入框，销售方式仍是下拉
        layout = [
            (0, 0, "ordertype"), (0, 2, "orderorign"), (0, 4, "plantype"),
            (1, 0, "skucode"), (1, 2, "skuname"), (1, 4, "salesmode"),
        ]
        labels = {field: label for field, label, _mode in q.FILTER_FIELDS}

        for row, column, field in layout:
            ttk.Label(box, text=labels[field]).grid(
                row=row, column=column, sticky="e", padx=(0 if column == 0 else 12, 6), pady=4
            )
            var = tk.StringVar(value=ALL if field != "skucode" and field != "skuname" else "")
            self.filter_vars[field] = var

            if field in ("skucode", "skuname"):
                entry = ttk.Entry(box, textvariable=var, width=18)
                entry.grid(row=row, column=column + 1, sticky="ew", pady=4)
                # 输入框里回车等于点查询
                entry.bind("<Return>", lambda _event: self.on_search())
            else:
                combo = ttk.Combobox(box, textvariable=var, width=16, state="readonly")
                combo.grid(row=row, column=column + 1, sticky="ew", pady=4)
                self.filter_combos[field] = combo

        # 分类：页面是一个三级级联，这里拆成三个联动下拉
        for index, (field, label) in enumerate(q.CATEGORY_FIELDS):
            ttk.Label(box, text=label).grid(
                row=2, column=index * 2, sticky="e",
                padx=(0 if index == 0 else 12, 6), pady=4,
            )
            var = tk.StringVar(value=ALL)
            self.filter_vars[field] = var
            combo = ttk.Combobox(box, textvariable=var, width=16, state="readonly")
            combo.grid(row=2, column=index * 2 + 1, sticky="ew", pady=4)
            self.filter_combos[field] = combo
            # 上级一变，下级候选跟着收窄
            combo.bind("<<ComboboxSelected>>", lambda _event, f=field: self._on_category_change(f))

        actions = ttk.Frame(box)
        actions.grid(row=3, column=0, columnspan=6, sticky="e", pady=(10, 0))
        ttk.Button(actions, text="查询", command=self.on_search, width=10).pack(side="left")
        ttk.Button(actions, text="重置", command=self.on_reset, width=10).pack(
            side="left", padx=6
        )
        self.export_button = ttk.Button(
            actions, text="导出 CSV…", command=self.on_export, width=12
        )
        self.export_button.pack(side="left")
        return box

    def _build_table(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.Frame(parent)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        columns = [field for field, _label, _width in q.PAGE_COLUMNS]
        self.tree = ttk.Treeview(box, columns=columns, show="headings", height=16,
                                 style="Qdama.Treeview")
        for field, label, width in q.PAGE_COLUMNS:
            self.tree.heading(field, text=label)
            self.tree.column(field, width=width, minwidth=70, anchor="center", stretch=False)

        vscroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        hscroll = ttk.Scrollbar(box, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")

        # 隔行浅色，一行行看过去不容易串行。
        # **前景也要给**：只给背景的话，深色模式下文字色是系统白，白字压浅底
        # 整行糊掉——这是上一版最直接的翻车点。
        self.tree.tag_configure("odd", background=ROW_ODD, foreground=TEXT)
        self.tree.tag_configure("even", background=ROW_EVEN, foreground=TEXT)
        return box

    def _build_footer(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.Frame(parent)

        self.sum_items_var = tk.StringVar(value="合计品项：0")
        self.sum_qty_var = tk.StringVar(value="订购数量：0.00")
        ttk.Label(box, textvariable=self.sum_items_var).pack(side="left")
        ttk.Label(box, textvariable=self.sum_qty_var).pack(side="left", padx=(18, 0))

        ttk.Label(box, text="每页").pack(side="left", padx=(24, 4))
        self.size_var = tk.StringVar(value=str(self.size))
        size_combo = ttk.Combobox(
            box, textvariable=self.size_var, width=5, state="readonly",
            values=[str(item) for item in PAGE_SIZES],
        )
        size_combo.pack(side="left")
        size_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_size_change())

        ttk.Button(box, text="‹", width=3, command=lambda: self._go_page(-1)).pack(
            side="left", padx=(12, 2)
        )
        ttk.Button(box, text="›", width=3, command=lambda: self._go_page(1)).pack(side="left")

        self.page_var = tk.StringVar(value="第 1 / 1 页")
        ttk.Label(box, textvariable=self.page_var).pack(side="left", padx=(8, 0))
        self.count_var = tk.StringVar(value="共 0 条")
        ttk.Label(box, textvariable=self.count_var, foreground=MUTED).pack(side="left", padx=(12, 0))

        self.status_tab_var = tk.StringVar(value=ALL)
        tabs = ttk.Frame(box)
        tabs.pack(side="right")
        ttk.Label(tabs, text="订单状态").pack(side="left", padx=(0, 8))
        for name in q.STATUS_TABS:
            ttk.Radiobutton(
                tabs, text=name, value=name, variable=self.status_tab_var,
                command=self.on_status_change,
            ).pack(side="left", padx=(0, 6))
        return box

    def _build_status(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.Frame(parent)
        box.columnconfigure(0, weight=1)
        self.msg_var = tk.StringVar(value="就绪")
        ttk.Label(box, textvariable=self.msg_var, anchor="w", foreground=TEXT).grid(
            row=0, column=0, sticky="ew"
        )
        self.progress = ttk.Progressbar(box, mode="indeterminate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        return box

    # -------------------------------------------------------- 线程与事件循环

    def _emit(self, kind: str, payload: object = None) -> None:
        """后台线程唯一被允许做的事。"""
        self._queue.put((kind, payload))

    def _log(self, message: str) -> None:
        """把关键动作写到 stderr。

        GUI 本身不需要终端，但出问题时这是唯一的取证途径——界面上那句报错
        只存在于用户的截图里，而截图里通常没有完整堆栈。启动壳会把 stderr
        落到 `/tmp/qdama_gui.log`，所以"发日志给我"是一句能落地的话。

        自检模式（`--smoke` / `--auto` / `--live`）不打——自检自己会输出断言
        结果，混在一起反而难读。
        """
        if not self._autostart:
            return
        print(f"[qdama-gui] {message}", file=sys.stderr, flush=True)

    def _start_worker(self, target) -> None:
        threading.Thread(target=target, daemon=True).start()

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                self._dispatch(kind, payload)
        except queue.Empty:
            pass
        try:
            self.root.after(80, self._pump)
        except tk.TclError:
            pass                                # 窗口已经关了

    def _dispatch(self, kind: str, payload: object) -> None:
        if kind == EV_PROGRESS:
            self.msg_var.set(str(payload))
        elif kind == EV_ERROR:
            self._set_busy(False)
            message = str(payload)
            self._log(f"错误：{message.splitlines()[0]}")
            self.msg_var.set(message.splitlines()[0])
            if self._on_login_page():
                # 就在登录页上：错误直接写卡片上，别弹窗——登录失败本来就有
                # 一半概率，弹窗会让人以为程序坏了
                self.login_msg_var.set(message)
            else:
                messagebox.showerror("出错了", message)
                # token 坏掉了就把登录页摆回来，别让用户对着灰界面猜
                if "100031" in message or "100043" in message:
                    self._require_login("登录已失效，请重新登录。")
        elif kind == EV_ROWS:
            self._on_rows(payload)
        elif kind == EV_LOGIN_OK:
            self._on_login_ok(payload)
        elif kind == EV_SAVED_TOKEN_OK:
            self._on_saved_token_ok(payload)

    def _on_login_page(self) -> bool:
        try:
            return bool(self.login_frame.winfo_ismapped())
        except tk.TclError:
            return False

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = ["disabled"] if busy else ["!disabled"]
        for widget in (self.refresh_button, self.export_button, self.login_button):
            widget.state(state)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    # ------------------------------------------------------------ 界面切换

    def _show_login(self) -> None:
        self.main_frame.pack_forget()
        self.login_frame.pack(fill="both", expand=True)

    def _show_main(self) -> None:
        self.login_frame.pack_forget()
        self.main_frame.pack(fill="both", expand=True)

    def _require_login(self, message: str = "", focus_password: bool = False) -> None:
        self._show_login()
        self.login_msg_var.set(message)
        # 账号已填好的话直接跳密码框，少按一次 Tab
        target = self.login_pass_entry if focus_password else self.login_user_entry
        self.root.after(50, target.focus_set)

    # -------------------------------------------------------------- 启动流程

    def _prepare_login(self) -> None:
        """启动只做一件事：把登录页摆好。**不自动进主界面。**

        即使用户本机已经存着 token，也停在登录页——要账号密码换到新 token
        并落盘成功才放行（见 ``_on_login_ok``）。已有的 token 只作为"逃生口"
        挂在那儿，点一下会先验有效性再进。
        """
        saved, source = q.resolve_token("", "")
        if saved:
            self.token_button.grid()
            self.token_button.configure(
                text=f"用本地已保存的 token 进入（{source}）")
            self.login_msg_var.set("")
        else:
            self.token_button.grid_remove()
        self._require_login("", focus_password=bool(self.user_var.get().strip()))

    def _describe_session(self) -> str:
        nickname = self.session.get("nickname") or ""
        account = self.session.get("login_account") or ""
        who = " ".join(part for part in (nickname, account) if part)
        return who or "已用本地 token"

    def _load_session_shops(self) -> None:
        """把 session 里的门店灌进那两个下拉。

        「权限门店」是 ``X-QDM-Shop-Id`` 的来源，必须是账号有权访问的门店，所以
        只能从登录返回的 ``shopselects`` 里挑——不做成自由输入，免得又填出 100006。
        「查询门店」反过来，可以是任意门店，所以留着可编辑。
        """
        shops = self.session.get("shops") or []
        displays: list[str] = []
        sap_ids: list[str] = []
        for shop in shops:
            shop_id = str(shop.get("shopid") or "")
            if not shop_id:
                continue
            display = f"{shop_id} {shop.get('shopname') or ''}".strip()
            self._context_display_to_id[display] = shop_id
            displays.append(display)
            sap = str(shop.get("sapshopid") or "")
            if not sap:
                # 旧版 session 没存 sapshopid（2026-09-23 之前 login 的），只能拿
                # shopid 兜底。但内部编码是纯数字的（如 205082），**不是** SAP 码，
                # 填进查询参数服务端会回 100006「您没有该门店操作权限」——这个报错
                # 文案极具误导性，所以宁可什么都不给，让用户自己敲 SAP 码。
                sap = shop_id if not shop_id.isdigit() else ""
            if sap and sap not in sap_ids:
                sap_ids.append(sap)

        self.context_combo["values"] = displays
        self.store_combo["values"] = sap_ids

        session_shop = str(self.session.get("shop_id") or "")
        if session_shop:
            for display, shop_id in self._context_display_to_id.items():
                if shop_id == session_shop:
                    self.context_var.set(display)
                    break
            else:
                self.context_var.set(session_shop)
        if not self.store_var.get():
            # 同样地，纯数字的 shop_id 不能当查询门店用（见上面的兜底说明）
            fallback = str(self.session.get("sapshopid") or "")
            if not fallback and session_shop and not session_shop.isdigit():
                fallback = session_shop
            self.store_var.set(fallback or (sap_ids[0] if sap_ids else ""))

    def _context_shop_id(self) -> str:
        """「权限门店」下拉里的显示文本还原成 shopid。"""
        raw = self.context_var.get().strip()
        return self._context_display_to_id.get(raw, raw)

    # ------------------------------------------------------------------ 登录

    def on_login(self) -> None:
        if self._busy:
            return
        user = self.user_var.get().strip()
        password = self.pass_var.get()
        if not user:
            self.login_msg_var.set("先填账号。")
            return
        if not password:
            self.login_msg_var.set("先填密码。")
            return

        self.login_msg_var.set("")
        self._set_busy(True)
        self.msg_var.set("正在登录…")

        def work() -> None:
            try:
                # verbose=False：那些"公钥 2048 bit…"是命令行给人看的，
                # 在 GUI 里只会污染日志
                data = self._login_fn(user, password, domain=q.DEFAULT_DOMAIN,
                                      tenant=q.DEFAULT_TENANT, verbose=False)
            except q.ApiError as error:
                self._log(f"登录被拒：{str(error).splitlines()[0]}")
                self._emit(EV_ERROR, f"{error}\n\n注意：连错 5 次会锁定账号。")
                return
            except Exception as error:           # noqa: BLE001
                self._log(f"登录异常：{type(error).__name__}: {error}")
                self._emit(EV_ERROR, f"登录失败：{error}")
                return
            self._emit(EV_LOGIN_OK, data)

        self._start_worker(work)

    def _on_login_ok(self, data: dict) -> None:
        """登录成功 → **先把 token 存下来** → 存成功才切到列表页。

        顺序是有意的：token 落盘失败就停在登录页并说明原因，不放行。
        这样"看到列表页"这件事本身就等于"token 已经安全落地了"。
        """
        self.pass_var.set("")                    # 用完就丢，别留在内存里
        token = data.get("token") or ""
        if not token:
            self._set_busy(False)
            self._log("登录返回里没有 token 字段")
            self.login_msg_var.set("服务端没有返回 token，登录不算成功。")
            return

        token_path = os.path.expanduser(q.DEFAULT_TOKEN_PATH)
        try:
            with open(token_path, "w", encoding="utf-8") as handle:
                handle.write(token)
            os.chmod(token_path, 0o600)          # 里面是凭证，别让同机其他用户读
            self._log(f"登录成功，token {len(token)} 字符已写入 {token_path}")
        except OSError as error:
            self._set_busy(False)
            self._log(f"token 写盘失败：{error}")
            self.login_msg_var.set(
                f"token 没能保存到 {token_path}：\n{error}\n"
                "本地存不下来就不进入列表页，请检查目录权限或先用其他路径。")
            return

        self.token = token
        self.token_source = token_path
        self.session = q.session_from_login(data, q.DEFAULT_DOMAIN)
        try:
            q.save_session(self.session)
        except OSError:
            pass

        self._enter_main(f"登录成功，token 已保存到 {token_path}；正在拉数据…")

    def _on_saved_token_ok(self, payload: tuple[str, str]) -> None:
        """走"本地已有 token"这条路的收尾。token 是现成的，不用再存一次。"""
        self.token, self.token_source = payload
        self.session = q.load_session()
        self._enter_main(f"本地 token 可用（{self.token_source}）；正在拉数据…")

    def _enter_main(self, message: str) -> None:
        """从登录页切到列表页，并立刻拉一次数据。"""
        self.who_var.set(self._describe_session() or self.user_var.get().strip())
        self._save_config()
        self._load_session_shops()              # 门店下拉要换成这个账号的门店
        self._set_busy(False)
        self._show_main()
        self.msg_var.set(message)
        self.on_refresh()

    def on_use_saved_token(self) -> None:
        """逃生口：本机已有 token 文件时跳过登录。

        不是无脑放行——先拿它打一次需要登录态的接口，确认有效再进，
        否则会把一个废 token 带进列表页，然后满屏 100031。
        """
        if self._busy:
            return
        token, source = q.resolve_token("", "")
        if not token:
            self.token_button.grid_remove()
            self.login_msg_var.set("本机找不到可用的 token 文件，请用账号密码登录。")
            return

        self.login_msg_var.set("")
        self._set_busy(True)
        self.msg_var.set("正在校验本地 token…")
        session = self.session or {}

        def work() -> None:
            try:
                q.check_permission(
                    token, list(q.DEFAULT_PERMISSIONS), domain=q.DEFAULT_DOMAIN,
                    tenant=session.get("tenant") or q.DEFAULT_TENANT,
                    shop_id=session.get("shop_id") or "",
                    sys_user_id=session.get("sys_user_id") or "",
                )
            except q.ApiError as error:
                self._emit(EV_ERROR, f"本地 token 已失效：{error}\n"
                                     "请用账号密码重新登录。")
                return
            except Exception as error:           # noqa: BLE001
                self._emit(EV_ERROR, f"校验本地 token 失败：{error}")
                return
            self._emit(EV_SAVED_TOKEN_OK, (token, source))

        self._start_worker(work)

    def on_logout(self) -> None:
        """退出登录：清掉内存里的 token，并把落盘的 token 删掉。

        不删的话，下次启动又会拿这个已经登出的 token 去请求。
        """
        self.token = ""
        self.token_source = "(未提供)"
        self.all_rows = []
        self.view_rows = []
        self._render()
        for path in q.TOKEN_SEARCH_PATHS:
            full = os.path.expanduser(path)
            try:
                if os.path.isfile(full):
                    os.remove(full)
            except OSError:
                pass
        self.who_var.set("未登录")
        # token 文件已删，那个逃生口也就没意义了
        self.token_button.grid_remove()
        self._require_login("已退出登录。")

    # ------------------------------------------------------------------ 取数

    def on_refresh(self) -> None:
        if self._busy:
            return
        store = self.store_var.get().strip()
        if not store:
            messagebox.showinfo("缺门店", "先填门店 SAP 编码（形如 A25V）。")
            return
        try:
            date = self._api_date()
        except ValueError:
            messagebox.showinfo("日期不对", "到店日期要是 2026-09-23 这种格式。")
            return

        self._save_config()
        self._set_busy(True)
        self.msg_var.set(f"正在拉 {date} 的数据…")
        self._log(f"取数：{date} 查询门店={store}")
        session = self.session or {}
        # tk 变量只能在主线程碰，所以先把要用的值取出来，再交给后台线程
        context_shop = self._context_shop_id() or str(session.get("shop_id") or "")
        tenant = str(session.get("tenant") or q.DEFAULT_TENANT)
        sys_user_id = str(session.get("sys_user_id") or "")
        token = self.token or q.resolve_token("", "")[0]

        def work() -> None:
            try:
                # token 可能来自启动时的探测，这里再解析一次保证用的是最新值
                current = token or q.resolve_token("", "")[0]
                part = self._fetcher(
                    date, store, current,
                    domain=q.DEFAULT_DOMAIN,
                    tenant=tenant,
                    shop_id=context_shop,
                    sys_user_id=sys_user_id,
                )
            except q.ApiError as error:
                self._emit(EV_ERROR, str(error))
                return
            except Exception as error:           # noqa: BLE001
                self._emit(EV_ERROR, f"取数失败：{error}")
                return
            self._emit(EV_ROWS, part)

        self._start_worker(work)

    def _on_rows(self, part: dict) -> None:
        self.all_rows = part.get("purchaseordersummarylist") or []
        self.server_summary = {
            "totalskuqty": part.get("totalskuqty") or 0,
            "totalorderqty": part.get("totalorderqty") or "0.00",
        }
        self._log(f"取数成功：{len(self.all_rows)} 行，"
                  f"合计品项 {self.server_summary['totalskuqty']}，"
                  f"订购数量 {self.server_summary['totalorderqty']}")
        self._set_busy(False)
        self._refresh_filter_options()
        self.page = 1
        self._recompute()
        self.msg_var.set(
            f"{self._api_date()} · 门店 {self.store_var.get().strip()} · "
            f"拉到 {len(self.all_rows)} 条"
            f"（服务端合计：品项 {self.server_summary['totalskuqty']}，"
            f"数量 {self.server_summary['totalorderqty']}）"
        )

    def _api_date(self) -> str:
        """界面上的 yyyy-MM-dd 转成接口要的 yyyyMMdd。"""
        raw = self.date_var.get().strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return dt.datetime.strptime(raw, fmt).strftime("%Y%m%d")
            except ValueError:
                continue
        raise ValueError(raw)

    def _set_date(self, days_ago: int) -> None:
        target = dt.date.today() - dt.timedelta(days=days_ago)
        self.date_var.set(target.strftime("%Y-%m-%d"))
        self.on_refresh()

    # ------------------------------------------------------------ 筛选与渲染

    def _current_params(self) -> dict:
        """界面上的条件 → 传给 filter_rows 的字典。

        销售方式特殊：下拉里显示中文（"次日达"），过滤要比数字码（"40"）。
        """
        params: dict[str, str] = {}
        for field, _label, _mode in q.FILTER_FIELDS:
            value = self.filter_vars[field].get().strip()
            if field == "salesmode":
                value = self._sales_display_to_code.get(value, "")
            params[field] = "" if value == ALL else value
        for field, _label in q.CATEGORY_FIELDS:
            value = self.filter_vars[field].get().strip()
            params[field] = "" if value == ALL else value
        return params

    def _recompute(self, *, reset_page: bool = False) -> None:
        if reset_page:
            self.page = 1
        self.view_rows = apply_filters(
            self.all_rows, self._current_params(), self.status_tab_var.get()
        )
        self._render()

    def _render(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        total = len(self.view_rows)
        pages = max(1, (total + self.size - 1) // self.size)
        self.page = min(max(1, self.page), pages)
        window = q.paginate(self.view_rows, self.page, self.size)

        fields = [field for field, _label, _width in q.PAGE_COLUMNS]
        for index, row in enumerate(window):
            self.tree.insert(
                "", "end",
                values=[cell_text(row, field) for field in fields],
                # 每行都打标签（含偶数行）——不打标签的行会回落到主题默认色，
                # 那正是深色模式下变白字的原因
                tags=("odd" if index % 2 else "even",),
            )

        # 合计跟"我筛出来的这些行"对齐，不跟服务端那个总数较劲
        summary = q.summarize(self.view_rows)
        self.sum_items_var.set(f"合计品项：{summary['totalskuqty']}")
        self.sum_qty_var.set(f"订购数量：{summary['totalorderqty']}")
        self.page_var.set(f"第 {self.page} / {pages} 页")
        scope = "全部" if total == len(self.all_rows) else f"筛出 {total}"
        self.count_var.set(f"共 {total} 条（{scope}，原始 {len(self.all_rows)} 条）")

    def _on_size_change(self) -> None:
        try:
            self.size = int(self.size_var.get())
        except ValueError:
            self.size = PAGE_SIZES[0]
        self._save_config()
        self._recompute(reset_page=True)

    def _go_page(self, delta: int) -> None:
        total = len(self.view_rows)
        pages = max(1, (total + self.size - 1) // self.size)
        target = min(max(1, self.page + delta), pages)
        if target != self.page:
            self.page = target
            self._render()

    def on_status_change(self) -> None:
        self._recompute(reset_page=True)

    def on_search(self) -> None:
        self._recompute(reset_page=True)

    def on_reset(self) -> None:
        for field, _label, _mode in q.FILTER_FIELDS:
            self.filter_vars[field].set(ALL if field in self.filter_combos else "")
        for field, _label in q.CATEGORY_FIELDS:
            self.filter_vars[field].set(ALL)
        self.status_tab_var.set(ALL)
        self._refresh_category_options()
        self._recompute(reset_page=True)

    # -------------------------------------------------------- 下拉选项的维护

    def _refresh_filter_options(self) -> None:
        """下拉选项 = 页面上写死的那些 ∪ 数据里实际出现的值。

        只用页面那份，服务端加个新枚举就会选不到；只用数据那份，某些选项会在
        没数据时凭空消失。合起来两头都不吃亏。
        """
        for field, _label, _mode in q.FILTER_FIELDS:
            # 商品编码 / 名称是输入框，没有选项可填；销售方式用固定字典，见下
            if field not in self.filter_combos or field == "salesmode":
                continue
            seen = {str(row.get(field) or "").strip() for row in self.all_rows}
            seen.discard("")
            options = [ALL, *q.FILTER_OPTIONS.get(field, ())]
            options += [value for value in sorted(seen) if value not in options]
            self.filter_combos[field]["values"] = options

        # 销售方式：页面就是整个字典全列出来，不按数据收窄
        self._sales_display_to_code = {ALL: ""}
        options = [ALL]
        for code, label in q.SALES_MODE_LABELS.items():
            self._sales_display_to_code[label] = code
            options.append(label)
        self.filter_combos["salesmode"]["values"] = options

        self._refresh_category_options()

    def _refresh_category_options(self) -> None:
        def selected(field: str) -> str:
            value = self.filter_vars[field].get().strip()
            return "" if value == ALL else value

        choices = category_choices(
            self.all_rows, big=selected("bigcategoryname"), mid=selected("midcategoryname")
        )
        for field, _label in q.CATEGORY_FIELDS:
            values = self.filter_combos[field]["values"]
            keep = self.filter_vars[field].get()
            self.filter_combos[field]["values"] = [ALL, *choices[field]]
            # 上级改了下级可能就不存在了，那就退回「全部」，别留个选不中的值
            if keep not in self.filter_combos[field]["values"]:
                self.filter_vars[field].set(ALL)

    def _on_category_change(self, field: str) -> None:
        if field == "bigcategoryname":
            self.filter_vars["midcategoryname"].set(ALL)
            self.filter_vars["subcategoryname"].set(ALL)
        elif field == "midcategoryname":
            self.filter_vars["subcategoryname"].set(ALL)
        self._refresh_category_options()

    def _refresh_store_hint(self) -> None:
        store = self.store_var.get().strip()
        context = self._context_shop_id()
        if store and context and store != context:
            self.msg_var.set(
                f"查询门店 {store}，以 {context} 的身份查。"
                "查别的门店不用重新登录；但权限门店必须在账号授权范围内。"
            )
        elif store:
            self.msg_var.set(f"查询门店 {store}")

    # ------------------------------------------------------------------ 导出

    def on_export(self) -> None:
        if not self.view_rows:
            messagebox.showinfo("没数据", "先拉一次数据再导出。")
            return
        default = f"qdama-日常订购-{self._api_date()}.csv"
        path = filedialog.asksaveasfilename(
            title="导出当前筛选结果",
            defaultextension=".csv",
            initialdir=os.path.expanduser(OUTPUT_DIR),
            initialfile=default,
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        try:
            count = rows_to_csv(path, self.view_rows)
        except OSError as error:
            messagebox.showerror("写不进去", str(error))
            return
        self.msg_var.set(f"已导出 {count} 行 → {path}")

    # ------------------------------------------------------------------ 入口

    def run(self) -> None:
        self.root.mainloop()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qdama-gui",
        description="钱大妈「日常订购」图形界面。不带参数运行就是开窗体。",
    )
    parser.add_argument("--shop", help="预填门店 SAP 编码（要查哪个门店）")
    parser.add_argument("--shop-id", help="预填权限门店（X-QDM-Shop-Id）；--live 时也会用")
    parser.add_argument("--date", help="预填到店日期（yyyy-MM-dd）")
    parser.add_argument("--user", help="预填登录账号")
    parser.add_argument("--config", default=CONFIG_PATH, help="界面偏好文件路径")
    parser.add_argument(
        "--smoke", action="store_true",
        help="只构建窗口然后退出，用于自检（不需要人盯着）",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="无人值守：用一批假数据走完整条界面链路，再从表格里读回来核对",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="用真实 token 拉一天真数据，核对界面渲染与接口返回是否一致",
    )
    return parser


def _demo_rows(count: int = 37) -> list[dict]:
    """自检用的假数据。字段名照抄接口，让筛选和格式化都能真跑一遍。"""
    types = ["日常订单", "紧急加单", "爆款订单", "赠品订单"]
    origins = ["门店订购", "线上订单", "总部代订", "中台加单"]
    plans = ["门店商品订购", "电商订购", "物料订购"]
    sales = [("10", "门店"), ("40", "次日达"), ("30", "预售")]
    rows = []
    for index in range(count):
        mode_code, mode_label = sales[index % len(sales)]
        rows.append({
            "arrivaldate": "2026-09-23",
            "skucode": f"{20000000 + index}",
            "skuname": f"测试商品{index:02d}",
            "salesmode": mode_code,
            "salesmodedesc": mode_label,
            "bigcategoryname": ["乳品", "生鲜"][index % 2],
            "midcategoryname": ["低温奶", "蛋品"][index % 2],
            "subcategoryname": ["鲜奶类", "鸡蛋类", "大闸蟹类"][index % 3],
            "orderqty": f"{index % 5 + 1}.000",
            "orderunit": ["盒", "千克", "袋"][index % 3],
            "ordertype": types[index % len(types)],
            "plantype": plans[index % len(plans)],
            "orderorign": origins[index % len(origins)],
            "orderstatus": "已提交" if index % 7 else "已删除",
            "comboflag": "1" if index % 11 == 0 else "0",
            "remark": "",
        })
    return rows


def _autorun(root: tk.Tk, app: QdamaApp, args: argparse.Namespace) -> int:
    """无人值守跑一遍界面链路，然后**从真实的 Treeview 里**把数据读回来核对。

    断言读的是控件里真实存在的行，不是某个中间变量——「结果存进了变量」和
    「结果真的显示出来了」是两件事，前者骗得过自己。
    """
    rows = _demo_rows()
    app.token = "FAKE-TOKEN"
    app.session = {
        "shop_id": "205082", "sys_user_id": "136387", "tenant": q.DEFAULT_TENANT,
        "sapshopid": "A3VP", "nickname": "自检账号",
        "shops": [
            {"shopid": "205082", "sapshopid": "A3VP", "shopname": "东莞地标广场"},
            {"shopid": "A3VP", "sapshopid": "A3VP", "shopname": "上海申江豪城"},
        ],
    }
    app._load_session_shops()
    app.store_var.set(args.shop or "A3VP")
    app.who_var.set("自检")

    app._on_rows({
        "purchaseordersummarylist": rows,
        "totalskuqty": len({row["skucode"] for row in rows}),
        "totalorderqty": f"{sum(float(row['orderqty']) for row in rows):.2f}",
    })
    root.update_idletasks()
    root.update()

    failures = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        failures += 0 if condition else 1
        print(f"  {'✓' if condition else '✗'} {label}{' → ' + detail if detail else ''}")

    def refresh() -> None:
        root.update_idletasks()
        root.update()

    columns = list(app.tree["columns"])
    check("表格列 = 页面 13 列，顺序一致",
          columns == [field for field, _l, _w in q.PAGE_COLUMNS],
          f"{len(columns)} 列")
    check("表头文字与页面一致",
          [app.tree.heading(field, "text") for field in columns[:3]] == ["到店日期", "商品编码", "商品名称"])

    shown = len(app.tree.get_children())
    check("默认每页 10 条，界面上就是 10 行", shown == 10, f"实际 {shown} 行")

    # 第 2 页
    app._go_page(1)
    refresh()
    check("翻到第 2 页仍有数据", len(app.tree.get_children()) == 10)
    check("页码标签跟着走", app.page_var.get().startswith("第 2 /"), app.page_var.get())
    app._go_page(-1)
    refresh()

    # 组合套餐的显示规矩：只有 1 显示"是"
    combo_column = columns.index("comboflag")
    values = {app.tree.item(item, "values")[combo_column] for item in app.tree.get_children()}
    check("组合套餐只显示「是」或空白", values <= {"是", ""}, str(sorted(values)))

    # 筛选：订单类型
    total_before = len(app.view_rows)
    app.filter_vars["ordertype"].set("日常订单")
    app.on_search()
    refresh()
    expected = len([row for row in rows if row["ordertype"] == "日常订单"])
    check("按订单类型筛选（码值精确）", len(app.view_rows) == expected,
          f"{total_before} → {len(app.view_rows)}，期望 {expected}")

    # 筛选：商品名称子串
    app.on_reset()
    refresh()
    app.filter_vars["skuname"].set("商品1")
    app.on_search()
    refresh()
    expected = len([row for row in rows if "商品1" in row["skuname"]])
    check("按商品名称筛选（子串）", len(app.view_rows) == expected,
          f"{len(app.view_rows)} 行，期望 {expected}")

    # 状态切换
    app.on_reset()
    refresh()
    app.status_tab_var.set("已删除")
    app.on_status_change()
    refresh()
    expected = len([row for row in rows if row["orderstatus"] == "已删除"])
    check("状态切换：已删除", len(app.view_rows) == expected,
          f"{len(app.view_rows)} 行，期望 {expected}")

    # 合计
    app.on_reset()
    refresh()
    summary = q.summarize(app.view_rows)
    check("合计品项 = 商品编码去重数",
          app.sum_items_var.get() == f"合计品项：{summary['totalskuqty']}",
          app.sum_items_var.get())
    check("合计数量与逐行求和一致",
          app.sum_qty_var.get() == f"订购数量：{summary['totalorderqty']}",
          app.sum_qty_var.get())

    # 分类联动
    app.filter_vars["bigcategoryname"].set("生鲜")
    app._on_category_change("bigcategoryname")
    refresh()
    mid_values = set(app.filter_combos["midcategoryname"]["values"])
    check("选了大分类后，中分类只剩这一支", "蛋品" in mid_values and "低温奶" not in mid_values,
          str(sorted(mid_values)))

    # 导出
    csv_path = os.path.join(os.path.expanduser("~"), ".qdama_gui_selftest.csv")
    written = rows_to_csv(csv_path, app.view_rows[:3])
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle))
            body = list(csv.reader(handle))
        check("导出 CSV：表头是 13 个中文列名",
              header == [label for _f, label, _w in q.PAGE_COLUMNS] and len(header) == 13)
        check("导出 CSV：行数与写入一致", written == len(body) and written == 3, f"{written} 行")
    finally:
        try:
            os.remove(csv_path)
        except OSError:
            pass

    check("筛选有结果时表格里确实有行", len(app.tree.get_children()) > 0,
          f"{len(app.tree.get_children())} 行")

    # 配色：深色模式翻过车，这几条是防它再翻一次。
    # 只断言"我给的值"不够——主题可能在渲染时把值换掉，所以连 Style 解析出来的
    # 实际生效值一起查。
    style = ttk.Style()
    resolved_fg = style.lookup("Qdama.Treeview", "foreground")
    resolved_bg = style.lookup("Qdama.Treeview", "background")
    check("表格前景色不是系统色（深色模式下会变成白字）",
          "system" not in str(resolved_fg).lower(), str(resolved_fg))
    check("表格背景色不是系统色", "system" not in str(resolved_bg).lower(), str(resolved_bg))

    odd_fg = app.tree.tag_configure("odd", "foreground")
    even_fg = app.tree.tag_configure("even", "foreground")
    check("隔行标签同时设了背景和前景",
          bool(odd_fg) and bool(even_fg)
          and str(odd_fg) == TEXT and str(even_fg) == TEXT,
          f"odd={odd_fg!r} even={even_fg!r}")

    item_tags = {app.tree.item(item, "tags")[0] for item in app.tree.get_children()}
    check("每一行都带隔行标签（无标签的行会回落到主题默认色）",
          item_tags and item_tags <= {"odd", "even"}, str(sorted(item_tags)))

    # 登录流程：启动时必须停在登录页，列表页不能自己冒出来
    check("启动停在登录页，而不是直接给列表页",
          bool(app.login_frame.winfo_ismapped()) and not app.main_frame.winfo_ismapped())
    check("登录按钮在主色按钮样式上",
          app.login_button.cget("style") == "Brand.TButton",
          app.login_button.cget("style"))
    check("密码框有掩码", app.login_pass_entry.cget("show") == "•")

    print(f"\n{'全部通过' if not failures else f'{failures} 项失败'}")
    return 1 if failures else 0


def _live_check(root: tk.Tk, app: QdamaApp, args: argparse.Namespace) -> int:
    """用真实 token 拉一天真数据，核对界面渲染与接口返回是否一致。

    假数据只能证明筛选逻辑自洽，**真数据才能证明我抄的列名、字段名、合计口径
    跟服务端对得上**。所以这个模式不能省。
    """
    token, source = q.resolve_token("", "")
    if not token:
        print("没有 token。先跑：python3 qdama_daily.py login --user <账号>",
              file=sys.stderr)
        return 2

    session = app.session or {}
    store = (args.shop or app.store_var.get().strip()
             or str(session.get("sapshopid") or session.get("shop_id") or ""))
    shop_id = (args.shop_id or app._context_shop_id()
               or str(session.get("shop_id") or ""))
    if not store:
        print("没有门店。加 --shop A3VP 之类的参数。", file=sys.stderr)
        return 2

    print(f"token 来源   {source}")
    print(f"查询门店     {store}   （sapshopid：查哪个门店）")
    print(f"权限门店     {shop_id or '(空)'}   （X-QDM-Shop-Id：以谁的身份查）")
    print()

    try:
        part = q.fetch_daily_page(
            app._api_date(), store, token,
            domain=q.DEFAULT_DOMAIN,
            tenant=str(session.get("tenant") or q.DEFAULT_TENANT),
            shop_id=shop_id,
            sys_user_id=str(session.get("sys_user_id") or ""),
        )
    except q.ApiError as error:
        print(f"取数失败：{error}", file=sys.stderr)
        return 1

    app.store_var.set(store)
    app._on_rows(part)
    root.update_idletasks()
    root.update()

    failures = 0

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        failures += 0 if condition else 1
        print(f"  {'✓' if condition else '✗'} {label}{' → ' + detail if detail else ''}")

    rows = app.all_rows
    print(f"接口返回 {len(rows)} 行\n")

    columns = list(app.tree["columns"])
    check("表格 13 列，列名与页面一致",
          columns == [field for field, _l, _w in q.PAGE_COLUMNS], f"{len(columns)} 列")
    check("每页 10 条，界面上就是 10 行",
          len(app.tree.get_children()) == min(app.size, len(rows)),
          f"{len(app.tree.get_children())} 行")

    # 关键交叉验证：本地按页面口径重算的合计，要与服务端给的数字对上。
    # 对不上就说明我抄的合计口径跟页面不是一回事，得回头查。
    local = q.summarize(rows)
    check("本地重算的合计品项 == 服务端 totalskuqty",
          str(local["totalskuqty"]) == str(part.get("totalskuqty")),
          f"本地 {local['totalskuqty']} / 服务端 {part.get('totalskuqty')}")
    server_qty = float(part.get("totalorderqty") or 0)
    check("本地重算的订购数量 == 服务端 totalorderqty",
          abs(float(local["totalorderqty"]) - server_qty) < 0.01,
          f"本地 {local['totalorderqty']} / 服务端 {part.get('totalorderqty')}")
    check("界面上的合计标签跟着走",
          app.sum_items_var.get() == f"合计品项：{local['totalskuqty']}",
          app.sum_items_var.get())

    # 逐格比对首行：表头对不代表单元格映射对，字段串位是很容易犯的错
    if rows:
        first = list(app.tree.item(app.tree.get_children()[0], "values"))
        expected = [cell_text(rows[0], field) for field, _l, _w in q.PAGE_COLUMNS]
        check("首行 13 列文本与原始数据逐格一致", first == expected)
        print(f"      首行：{first[:6]}")

    # 翻页
    if len(rows) > app.size:
        app._go_page(1)
        root.update_idletasks()
        check("翻到第 2 页仍有数据", len(app.tree.get_children()) > 0)
        app._go_page(-1)
        root.update_idletasks()

    # 状态切换：真数据一般全是「已提交」
    app.status_tab_var.set("已提交")
    app.on_status_change()
    root.update_idletasks()
    submitted = len([row for row in rows if str(row.get("orderstatus") or "") == "已提交"])
    check("状态切「已提交」与本地计数一致", len(app.view_rows) == submitted,
          f"{len(app.view_rows)} / {submitted}")

    # 挑一个真数据里存在的中文值来筛，验证码值精确匹配确实命中
    app.on_reset()
    root.update_idletasks()
    sample = next((str(row.get("ordertype") or "") for row in rows
                   if row.get("ordertype")), "")
    if sample:
        app.filter_vars["ordertype"].set(sample)
        app.on_search()
        root.update_idletasks()
        expected_count = len([row for row in rows if str(row.get("ordertype")) == sample])
        check(f"按真实订单类型「{sample}」筛选", len(app.view_rows) == expected_count,
              f"{len(app.view_rows)} / {expected_count}")
        check("筛选后表格里确实有行", len(app.tree.get_children()) > 0)
        app.on_reset()

    print(f"\n{'全部通过' if not failures else f'{failures} 项失败'}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 自检模式一律关掉自动取数：那个延迟回调会在断言中途拿真实数据把假数据顶掉，
    # 于是测出来的是"网络通不通"，而不是"界面逻辑对不对"。
    headless = args.smoke or args.auto or args.live

    root = tk.Tk()
    app = QdamaApp(root, config_path=args.config, autostart=not headless)

    if args.shop:
        app.store_var.set(args.shop)
    if args.shop_id:
        app.context_var.set(args.shop_id)
    if args.date:
        app.date_var.set(args.date)
    if args.user:
        app.user_var.set(args.user)

    if args.smoke:
        root.update_idletasks()
        root.update()
        print("GUI 构建成功。")
        root.destroy()
        return 0

    if args.auto:
        try:
            return _autorun(root, app, args)
        finally:
            root.destroy()

    if args.live:
        try:
            return _live_check(root, app, args)
        finally:
            root.destroy()

    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
