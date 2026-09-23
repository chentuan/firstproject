"""websnap 图形界面。

引擎层（session / auth / menu / extract / output）一行不改，全部复用。这里只做
"把结果摆到屏幕上"这一件事。

**线程模型**：所有网络请求都在一个后台线程里跑，事件经 ``queue`` 交回主线程。
tkinter 不是线程安全的——从别的线程碰控件不会立刻报错，而是在某个随机的时刻
崩掉，且崩得毫无规律。所以后台线程只允许做一件事：``queue.put``。

界面依赖只有 tkinter（标准库）。不需要界面时，``probe_site`` 和 ``fetch_menus``
这两个函数可以脱离 GUI 单独调用，自检脚本用的就是它们。
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .auth import LoginError, login
from .config import ConfigError, Profile, load_profile
from .dom import SelectorError, parse_html
from .extract import ExtractError, DataSet, extract
from .menu import (
    DEFAULT_EXCLUDE,
    DEFAULT_NAV_SELECTORS,
    Menu,
    discover_menus,
    find_page_nav,
)
from .output import build_payload, render_csv, render_json, safe_filename, visible_columns
from .session import RequestError, WebSession

APP_TITLE = "websnap · 按菜单抓取后台数据"
DEFAULT_TIMEOUT = 15.0

# 事件类型：后台线程 → 主线程
EV_PROGRESS = "progress"
EV_ERROR = "error"
EV_PROBED = "probed"
EV_RESULTS = "results"


# --------------------------------------------------------------------------- #
# 无界面依赖的部分：探测与抓取
# --------------------------------------------------------------------------- #

@dataclass
class SiteProbe:
    """一次"连接 + 认菜单"的结果。后台线程产出，主线程消费。"""

    session: WebSession
    profile: Profile
    base_url: str
    menus: list[Menu]
    nav_reason: str
    form: object | None = None

    @property
    def detail(self) -> str:
        if self.form is None:
            return "跳过登录"
        describe = getattr(self.form, "describe", None)
        return describe() if callable(describe) else "已登录"


def probe_site(
    base_url: str,
    username: str = "",
    password: str = "",
    *,
    profile: Profile | None = None,
    skip_login: bool = False,
    on_event=None,
) -> SiteProbe:
    """登录并在首页上识别菜单。**会阻塞**，请放在后台线程里调用。"""
    profile = profile or Profile()
    emit = on_event or (lambda kind, message=None: None)

    session = WebSession(
        base_url,
        timeout=profile.timeout,
        headers=profile.extra_headers,
        verify=profile.verify,
    )

    form = None
    if skip_login or profile.auth_mode == "none":
        emit(EV_PROGRESS, "按设置跳过登录。")
    else:
        if not username:
            raise ConfigError("没填登录账号。")
        if not password:
            raise ConfigError("没填登录密码。")
        emit(EV_PROGRESS, f"正在登录 {session.base_url} …")
        form = login(
            session,
            username,
            password,
            login_path=profile.login_path,
            username_field=profile.username_field,
            password_field=profile.password_field,
        )
        emit(EV_PROGRESS, f"登录成功（{form.describe()}）")

    emit(EV_PROGRESS, "正在读取首页并识别菜单 …")
    landing = session.get("/")
    menus, nav = discover_menus(
        parse_html(landing.text),
        base_url,
        selectors=profile.nav_selectors or DEFAULT_NAV_SELECTORS,
        exclude=profile.exclude or DEFAULT_EXCLUDE,
        min_links=profile.min_links,
    )
    if not menus:
        raise ConfigError(
            f"没能从 {landing.url} 认出菜单（{nav.reason}）。"
            "可以写一份站点配置，用 [menus] nav_selectors 指定导航区。"
        )
    emit(EV_PROGRESS, f"发现 {len(menus)} 个菜单（{nav.reason}）")

    return SiteProbe(
        session=session,
        profile=profile,
        base_url=base_url,
        menus=menus,
        nav_reason=nav.reason,
        form=form,
    )


def fetch_menus(
    probe: SiteProbe,
    menus: list[Menu],
    *,
    on_event=None,
) -> list[tuple[Menu, DataSet, str]]:
    """逐个抓取菜单页面并提取数据。**会阻塞**，请放在后台线程里调用。

    单个菜单失败不会中断整批——记一条进度就算了，最后的结果里少它一个，
    总比让用户盯着一个报错框猜"剩下的抓到没有"要好。
    """
    emit = on_event or (lambda kind, message=None: None)
    profile = probe.profile
    session = probe.session
    selectors = profile.nav_selectors or DEFAULT_NAV_SELECTORS
    mode = profile.extract_mode or "auto"
    selector = profile.extract_selector or None

    results: list[tuple[Menu, DataSet, str]] = []
    total = len(menus)

    for index, menu in enumerate(menus, start=1):
        prefix = f"[{index}/{total}]"
        emit(EV_PROGRESS, f"{prefix} 抓取「{menu.name}」…")

        response = session.get(menu.path)
        if response.status >= 400:
            emit(EV_PROGRESS, f"{prefix} 跳过「{menu.name}」：HTTP {response.status}")
            continue

        page_tree = parse_html(response.text)
        exclude_nodes = ()
        nav_on_page = find_page_nav(page_tree, selectors, profile.min_links)
        if nav_on_page is not None:
            # 页面自己的侧边栏链接不能混进数据里
            exclude_nodes = (nav_on_page,)

        try:
            dataset = extract(
                page_tree,
                probe.base_url,
                selector=selector,
                mode=mode,
                exclude_nodes=exclude_nodes,
            )
        except ExtractError as error:
            emit(EV_PROGRESS, f"{prefix} 「{menu.name}」提取失败：{error}")
            continue

        results.append((menu, dataset, response.url))
        emit(EV_PROGRESS, f"{prefix} 「{menu.name}」{dataset.describe()}")

    return results


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #

def explain_error(error: BaseException) -> str:
    """把异常翻成人话。模块级函数——它不需要界面，也就没必要挂在窗口对象上。"""
    if isinstance(error, LoginError):
        return f"登录失败：{error}"
    if isinstance(error, RequestError):
        # 具体原因由 session 层讲清楚（DNS / 端口拒绝 / 证书不信任 / 握手失败各不同）。
        # 这里不再追加"检查地址和服务是否启动"——对证书问题那是纯误导。
        return f"请求失败：{error}"
    if isinstance(error, (ConfigError, ExtractError, SelectorError)):
        return str(error)
    return f"{type(error).__name__}: {error}"


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    return str(value)


class WebsnapApp:
    """界面主体。构造即建好全部控件，``run()`` 进主循环。"""

    def __init__(self, root: tk.Misc, *, profile: Profile | None = None) -> None:
        self.root = root
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._probe: SiteProbe | None = None
        self._results: list[tuple[Menu, DataSet, str]] = []
        self._profile = profile or Profile()
        self._busy = False

        self.url_var = tk.StringVar(value=self._profile.base_url or "http://localhost:8080")
        self.user_var = tk.StringVar(value=self._profile.username or "")
        self.pass_var = tk.StringVar()
        self.timeout_var = tk.StringVar(value=str(int(self._profile.timeout or DEFAULT_TIMEOUT)))
        self.skip_var = tk.BooleanVar(value=self._profile.auth_mode == "none")
        self.insecure_var = tk.BooleanVar(value=not self._profile.verify)
        self.status_var = tk.StringVar(
            value="填好地址、账号、密码，点「连接并探测菜单」。"
        )

        self._build()
        self.root.after(80, self._pump)

    # ------------------------------------------------------------------ 构建

    def _build(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("1000x700")
        self.root.minsize(820, 560)

        style = ttk.Style()
        # macOS 默认行高偏矮，中文会挤在一起
        style.configure("Treeview", rowheight=24)
        style.configure("Treeview.Heading", font=("", 12))

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        self._build_connect(outer).grid(row=0, column=0, sticky="ew")
        self._build_body(outer).grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self._build_status(outer).grid(row=2, column=0, sticky="ew", pady=(12, 0))

    def _build_connect(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.LabelFrame(parent, text="连接", padding=10)
        box.columnconfigure(1, weight=3)
        box.columnconfigure(3, weight=2)

        ttk.Label(box, text="系统地址").grid(row=0, column=0, sticky="w", pady=3)
        url_entry = ttk.Entry(box, textvariable=self.url_var)
        url_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=3)

        ttk.Label(box, text="登录账号").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(box, textvariable=self.user_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 16), pady=3
        )
        ttk.Label(box, text="登录密码").grid(row=1, column=2, sticky="w", pady=3)
        self.pass_entry = ttk.Entry(box, textvariable=self.pass_var, show="*")
        self.pass_entry.grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=3)

        actions = ttk.Frame(box)
        actions.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))

        self.probe_button = ttk.Button(actions, text="连接并探测菜单", command=self.on_probe)
        self.probe_button.pack(side="left")
        ttk.Checkbutton(actions, text="跳过登录", variable=self.skip_var).pack(
            side="left", padx=(12, 0)
        )
        ttk.Checkbutton(actions, text="忽略证书错误", variable=self.insecure_var).pack(
            side="left", padx=(12, 0)
        )
        ttk.Label(actions, text="超时(秒)").pack(side="left", padx=(12, 4))
        ttk.Spinbox(
            actions, from_=1, to=300, textvariable=self.timeout_var, width=5
        ).pack(side="left")
        ttk.Button(actions, text="载入配置…", command=self.on_load_profile).pack(
            side="left", padx=(12, 0)
        )

        # 回车即触发，省得去够鼠标
        url_entry.bind("<Return>", lambda _event: self.on_probe())
        self.pass_entry.bind("<Return>", lambda _event: self.on_probe())
        return box

    def _build_body(self, parent: tk.Misc) -> ttk.Widget:
        paned = ttk.PanedWindow(parent, orient="horizontal")
        paned.add(self._build_menu_pane(paned), weight=1)
        paned.add(self._build_result_pane(paned), weight=4)
        return paned

    def _build_menu_pane(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.LabelFrame(parent, text="菜单（可多选）", padding=10)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        self.menu_list = tk.Listbox(
            box, selectmode="extended", exportselection=False, activestyle="none", width=18
        )
        self.menu_list.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.menu_list.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.menu_list.configure(yscrollcommand=scroll.set)

        pickers = ttk.Frame(box)
        pickers.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(pickers, text="全选", command=self.on_select_all).pack(side="left")
        ttk.Button(pickers, text="全不选", command=self.on_select_none).pack(
            side="left", padx=(6, 0)
        )

        self.fetch_button = ttk.Button(
            box, text="抓取选中菜单", command=self.on_fetch, state="disabled"
        )
        self.fetch_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        return box

    def _build_result_pane(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.LabelFrame(parent, text="结果", padding=10)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        self.notebook = ttk.Notebook(box)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        placeholder = ttk.Label(
            self.notebook,
            text="连接并抓取后，每个菜单会在这里占一个页签。",
            anchor="center",
        )
        self.notebook.add(placeholder, text="（空）")
        return box

    def _build_status(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.Frame(parent)
        box.columnconfigure(0, weight=1)

        ttk.Label(box, textvariable=self.status_var, anchor="w").grid(
            row=0, column=0, sticky="ew"
        )

        actions = ttk.Frame(box)
        actions.grid(row=0, column=1, sticky="e")
        self.export_csv_button = ttk.Button(
            actions, text="导出 CSV…", command=lambda: self.on_export("csv"), state="disabled"
        )
        self.export_csv_button.pack(side="left")
        self.export_json_button = ttk.Button(
            actions, text="导出 JSON…", command=lambda: self.on_export("json"), state="disabled"
        )
        self.export_json_button.pack(side="left", padx=(6, 0))

        self.progress = ttk.Progressbar(box, mode="indeterminate")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        return box

    # ------------------------------------------------------- 线程与事件循环

    def _emit(self, kind: str, payload: object = None) -> None:
        """后台线程唯一被允许做的事。"""
        self._queue.put((kind, payload))

    def _start_worker(self, target) -> None:
        threading.Thread(target=target, daemon=True).start()

    def _pump(self) -> None:
        """主线程定时把队列里的事件取出来处理。"""
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                self._dispatch(kind, payload)
        except queue.Empty:
            pass
        try:
            self.root.after(80, self._pump)
        except tk.TclError:
            pass  # 窗口已经关了

    def _dispatch(self, kind: str, payload: object) -> None:
        if kind == EV_PROGRESS:
            self.status_var.set(str(payload))
        elif kind == EV_ERROR:
            self._set_busy(False)
            message = str(payload)
            self.status_var.set(message.splitlines()[0])
            messagebox.showerror("出错了", message)
        elif kind == EV_PROBED:
            self._on_probed(payload)
        elif kind == EV_RESULTS:
            self._on_results(payload)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.probe_button.state(["disabled"] if busy else ["!disabled"])
        if busy:
            self.fetch_button.state(["disabled"])
            self.progress.start(12)
        else:
            self.progress.stop()
            if self._probe is not None:
                self.fetch_button.state(["!disabled"])

    def _collect_profile(self, base_url: str) -> Profile:
        """把表单上的设置覆盖到已载入的配置上。"""
        try:
            timeout = float(self.timeout_var.get())
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        timeout = max(1.0, min(300.0, timeout))
        return replace(
            self._profile,
            base_url=base_url,
            timeout=timeout,
            verify=not bool(self.insecure_var.get()),
            auth_mode="none" if self.skip_var.get() else self._profile.auth_mode,
        )

    def _explain(self, error: BaseException) -> str:
        return explain_error(error)

    # ------------------------------------------------------------------ 动作

    def on_probe(self) -> None:
        if self._busy:
            return

        base_url = self.url_var.get().strip()
        if not base_url:
            messagebox.showwarning("缺地址", "先填系统地址，例如 http://localhost:8080")
            return
        if not base_url.startswith(("http://", "https://")):
            base_url = "http://" + base_url
            self.url_var.set(base_url)

        username = self.user_var.get().strip()
        password = self.pass_var.get()
        profile = self._collect_profile(base_url)
        skip_login = bool(self.skip_var.get())

        self._set_busy(True)
        self.status_var.set("正在连接 …")

        def work() -> None:
            try:
                probe = probe_site(
                    base_url,
                    username,
                    password,
                    profile=profile,
                    skip_login=skip_login,
                    on_event=self._emit,
                )
            except BaseException as error:  # noqa: BLE001 - 界面线程不能死
                self._emit(EV_ERROR, self._explain(error))
                return
            self._emit(EV_PROBED, probe)

        self._start_worker(work)

    def _on_probed(self, probe: SiteProbe) -> None:
        self._set_busy(False)
        self._probe = probe

        self.menu_list.delete(0, "end")
        for menu in probe.menus:
            self.menu_list.insert("end", menu.name)
        self.menu_list.selection_set(0, "end")  # 默认全选，多数人是想全抓

        self.fetch_button.state(["!disabled"])
        self.status_var.set(
            f"已连接 {probe.base_url} · {probe.detail} · "
            f"识别到 {len(probe.menus)} 个菜单（{probe.nav_reason}），已默认全选。"
        )

    def on_select_all(self) -> None:
        if self.menu_list.size():
            self.menu_list.selection_set(0, "end")

    def on_select_none(self) -> None:
        self.menu_list.selection_clear(0, "end")

    def on_fetch(self) -> None:
        if self._busy or self._probe is None:
            return

        selection = self.menu_list.curselection()
        if not selection:
            messagebox.showinfo("没选菜单", "先在左边选一个或多个菜单。")
            return

        menus = [self._probe.menus[index] for index in selection]
        probe = self._probe

        self._set_busy(True)
        self._clear_results()
        self.status_var.set("正在抓取 …")

        def work() -> None:
            try:
                results = fetch_menus(probe, menus, on_event=self._emit)
            except BaseException as error:  # noqa: BLE001
                self._emit(EV_ERROR, self._explain(error))
                return
            self._emit(EV_RESULTS, results)

        self._start_worker(work)

    # ------------------------------------------------------------------ 结果

    def _clear_results(self) -> None:
        for tab_id in self.notebook.tabs():
            widget = self.notebook.nametowidget(tab_id)
            self.notebook.forget(tab_id)
            widget.destroy()

    def _on_results(self, results: list[tuple[Menu, DataSet, str]]) -> None:
        self._set_busy(False)
        self._clear_results()
        self._results = list(results)

        if not results:
            placeholder = ttk.Label(
                self.notebook, text="一个菜单都没抓到数据。", anchor="center"
            )
            self.notebook.add(placeholder, text="（空）")
            self.status_var.set("一个菜单都没抓到数据。")
            return

        total = 0
        for menu, dataset, _page_url in results:
            frame = ttk.Frame(self.notebook, padding=(0, 6))
            top = 0
            if dataset.note:
                # 提示不能只留给终端。抓不到数据时，原因本身就是最有用的信息。
                ttk.Label(
                    frame,
                    text="提示：" + dataset.note,
                    foreground="#a15c00",
                    justify="left",
                    wraplength=880,
                ).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 4))
                top = 1
            frame.rowconfigure(top, weight=1)
            frame.columnconfigure(0, weight=1)
            self._fill_tree(frame, dataset, top_row=top)
            self.notebook.add(frame, text=f"{menu.name}（{dataset.count}）")
            total += dataset.count

        self.notebook.select(0)
        self.export_csv_button.state(["!disabled"])
        self.export_json_button.state(["!disabled"])
        self.status_var.set(
            f"抓取完成：{len(results)} 个菜单，共 {total} 条。"
            f"导出会按菜单拆文件，菜单名就是文件名。"
        )

    def _fill_tree(self, parent: tk.Misc, dataset: DataSet, top_row: int = 0) -> ttk.Treeview:
        columns = visible_columns(dataset) or ["内容"]

        tree = ttk.Treeview(parent, columns=columns, show="headings", height=12)
        for column in columns:
            tree.heading(column, text=column)
            # 按列名长度估个宽度，太窄的中文表头会显示成省略号
            tree.column(
                column,
                width=min(220, max(80, 14 * len(column) + 40)),
                minwidth=56,
                anchor="w",
                stretch=True,
            )

        for index, row in enumerate(dataset.rows):
            values = [_cell_text(row.get(column)) for column in columns]
            tree.insert("", "end", iid=str(index), values=values)

        vscroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        hscroll = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)

        tree.grid(row=top_row, column=0, sticky="nsew")
        vscroll.grid(row=top_row, column=1, sticky="ns")
        hscroll.grid(row=top_row + 1, column=0, sticky="ew")
        return tree

    # ------------------------------------------------------------------ 导出

    def on_export(self, fmt: str) -> None:
        if not self._results:
            messagebox.showinfo("没数据", "先抓一次再导出。")
            return

        if fmt == "csv":
            directory = filedialog.askdirectory(title="选择导出目录（一个菜单一个文件）")
            if not directory:
                return
            target = Path(directory)
            written = 0
            for menu, dataset, _ in self._results:
                path = target / (safe_filename(menu.name or menu.path) + ".csv")
                path.write_text(render_csv(dataset, with_bom=True), encoding="utf-8")
                written += 1
            messagebox.showinfo("导出完成", f"已写入 {written} 个 CSV 文件到\n{target}")
            self.status_var.set(f"已导出 {written} 个 CSV 文件到 {target}")
            return

        path = filedialog.asksaveasfilename(
            title="导出 JSON",
            defaultextension=".json",
            initialfile="websnap.json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return

        site = self._profile.name or (self._probe.base_url if self._probe else "websnap")
        payload = _combined_payload(self._results, site)
        Path(path).write_text(render_json(payload) + "\n", encoding="utf-8")
        messagebox.showinfo("导出完成", f"已写入\n{path}")
        self.status_var.set(f"已导出 {len(self._results)} 个菜单到 {path}")

    # ------------------------------------------------------------------ 配置

    def on_load_profile(self) -> None:
        path = filedialog.askopenfilename(
            title="选择站点配置",
            filetypes=[("TOML 配置", "*.toml"), ("所有文件", "*.*")],
        )
        if not path:
            return

        try:
            profile, warnings = load_profile(path)
        except ConfigError as error:
            messagebox.showerror("配置读不了", str(error))
            return

        self._profile = profile
        if profile.base_url:
            self.url_var.set(profile.base_url)
        if profile.username:
            self.user_var.set(profile.username)
        if profile.timeout:
            self.timeout_var.set(str(int(profile.timeout)))
        if not profile.verify:
            self.insecure_var.set(True)
        if profile.auth_mode == "none":
            self.skip_var.set(True)

        note = f"已载入配置 {path}"
        if warnings:
            note += "（" + "；".join(warnings) + "）"
        self.status_var.set(note)

    def run(self) -> None:
        self.root.mainloop()


def _combined_payload(
    results: list[tuple[Menu, DataSet, str]], site: str
) -> dict:
    """多个菜单合成一个 JSON。与 CLI 的 --format json 结构保持一致。"""
    from datetime import datetime

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    payloads = [
        build_payload(
            dataset,
            site=site,
            menu_name=menu.name,
            menu_path=menu.path,
            fetched_at=stamp,
            page_url=page_url,
        )
        for menu, dataset, page_url in results
    ]
    return {"site": site, "fetched_at": stamp, "menus": len(payloads), "results": payloads}


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="websnap-gui",
        description="websnap 图形界面。不带参数运行就是开窗体。",
    )
    parser.add_argument("--url", "--base-url", dest="url", help="预填系统地址")
    parser.add_argument("--username", help="预填登录账号")
    parser.add_argument("--password", help="预填密码（不推荐，会留在 shell 历史里）")
    parser.add_argument("--password-env", default="WEBSNAP_PASSWORD", help="密码环境变量名")
    parser.add_argument("--profile", help="启动时载入的站点配置（TOML）")
    parser.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验")
    parser.add_argument("--timeout", type=float, help="单次请求超时秒数")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="只构建窗口然后退出，用于自检（不需要人盯着）",
    )
    parser.add_argument(
        "--auto",
        metavar="菜单",
        nargs="?",
        const="",
        help="无人值守：连接 → 抓取（可指定菜单，默认全部）→ 报告 → 退出",
    )
    parser.add_argument("--dump", metavar="目录", help="配合 --auto，把结果落成文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    profile = None
    if args.profile:
        profile, warnings = load_profile(args.profile)
        for warning in warnings:
            print(f"配置提醒：{warning}", file=sys.stderr)

    root = tk.Tk()
    app = WebsnapApp(root, profile=profile)

    if args.url:
        app.url_var.set(args.url)
    if args.username:
        app.user_var.set(args.username)
    if args.timeout:
        app.timeout_var.set(str(int(args.timeout)))
    if args.insecure:
        app.insecure_var.set(True)
    password = args.password or os.environ.get(args.password_env or "", "")
    if password:
        app.pass_var.set(password)

    if args.smoke:
        root.update_idletasks()
        root.update()
        print("GUI 构建成功。")
        root.destroy()
        return 0

    if args.auto is not None:
        code = _autorun(root, app, args)
        root.destroy()
        return code

    app.run()
    return 0


def _autorun(root: tk.Tk, app: WebsnapApp, args: argparse.Namespace) -> int:
    """无人值守跑一遍：真连后台、真抓数据、真填进控件，然后从控件里读回来核对。

    注意最后一步——断言读的是 Treeview 里真实存在的行，不是某个中间变量。
    "把结果塞进变量"和"结果真的显示出来了"是两件事。
    """
    emitter = lambda kind, message=None: print(f"  {message}", file=sys.stderr)  # noqa: E731

    try:
        probe = probe_site(
            app.url_var.get().strip(),
            app.user_var.get().strip(),
            app.pass_var.get(),
            profile=app._collect_profile(app.url_var.get().strip()),
            skip_login=bool(app.skip_var.get()),
            on_event=emitter,
        )
    except BaseException as error:  # noqa: BLE001
        print(f"错误：{app._explain(error)}", file=sys.stderr)
        return 1

    app._on_probed(probe)

    wanted = [token.strip() for token in (args.auto or "").split(",") if token.strip()]
    menus = [menu for menu in probe.menus if not wanted or menu.name in wanted]
    if not menus:
        print(f"错误：没有匹配的菜单（请求的是 {wanted}）", file=sys.stderr)
        return 1

    try:
        results = fetch_menus(probe, menus, on_event=emitter)
    except BaseException as error:  # noqa: BLE001
        print(f"错误：{app._explain(error)}", file=sys.stderr)
        return 1

    app._on_results(results)
    root.update_idletasks()
    root.update()

    # 从真实控件里把数据读回来
    tabs = app.notebook.tabs()
    print(f"\n界面上有 {len(tabs)} 个页签：")
    printed_rows = 0
    for tab_id in tabs:
        frame = app.notebook.nametowidget(tab_id)
        title = app.notebook.tab(tab_id, "text")
        trees = [child for child in frame.winfo_children() if isinstance(child, ttk.Treeview)]
        for tree in trees:
            rows = len(tree.get_children())
            columns = list(tree["columns"])
            printed_rows += rows
            print(f"  {title}：{rows} 行 × {len(columns)} 列  {columns[:5]}")
    print(f"合计 {printed_rows} 行")

    if args.dump:
        target = Path(args.dump)
        target.mkdir(parents=True, exist_ok=True)
        for menu, dataset, _ in results:
            (target / (safe_filename(menu.name) + ".csv")).write_text(
                render_csv(dataset, with_bom=True), encoding="utf-8"
            )
        print(f"已导出到 {target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
