#!/usr/bin/env python3
"""钱大妈智慧中台 · 日常订购数据导出（直连接口版）

为什么不能抓 HTML
-----------------
olpadmin.qdama.cn 是 Vue SPA：HTML 只是空壳，连登录表单都是 JS 生成的。
菜单、列表、分页全部走 XHR，只能直接调后端接口。

接口链路（逆向前端产物 /js/chunk-51c05ef0.e2b20925.js 得到）
-------------------------------------------------------------
页面上「每日订购」页（组件方法 total.getData）的真实调用：

    main.request(main.url.purchaseorderlistpurchaseordersummary,
                 {arrivaldate: "yyyyMMdd", sapshopid: "<门店SAP码>"},   // ← 第 2 参是 BODY
                 cb, "POST")                                            // ← 第 4 参是 METHOD

即 **POST + JSON body**，query 里只有 sign/nonce，**没有任何业务参数**：

    POST {api_base}sc/purchase/order/listpurchaseordersummary?sign=<签名>&nonce=<20位随机串>
    body  {"arrivaldate":"20260923","sapshopid":"A25V"}

    响应  {code: 0, data: {purchaseordersummarylist: [...], totalskuqty, totalorderqty}}

签名算法（两处真实样本 + 反编译源码双向对齐）
--------------------------------------------
    nonce = 20 位随机串，字符集 [0-9a-zA-Z]
    P     = 毫秒级时间戳，同时作为请求头 X-ZZ-Timestamp 发出去
    C     = query 非空 → 把(query + nonce)按 key 字典序拼成 key+value+key+value…
            query 为空 → 字面量 "nonce" + nonce
    tail  = body 非空 → JSON.stringify(body)，无空格、不转义非 ASCII
            body 为空 → ""
    sign  = md5(密钥 + C + P + tail)

    密钥由 document.domain 决定（见 ENVIRONMENTS），生产环境是 3Jr8S1K18rcC1wAfv8。

坑（踩过，写在这儿免得再踩）
----------------------------
1. **请求头 `v: 1.1` 是必需的。** 服务端靠它挑签名密钥的版本；不带这个头，
   无论签名算得多对，一律回 `100029 系统签名错误`。这一条卡了最久。
2. **业务参数放 body 还是 query，会改签名公式。** 本接口是 POST + body，如果照着
   「GET + query」的形状发（参数塞进 query、body 留空），签名照样能过校验，
   但服务端回 `100006 没有此功能使用权限`——形状不对，签名再对也没用。
3. `X-QDM-Shop-Id` 必须是**登录账号有权访问的门店**（登录返回 shopselects 里的那些），
   它和业务参数 `sapshopid`（决定查哪个门店）是两回事，两者不一定同值。
   填成 sapshopid、或填个账号没权限的门店，服务端一律回 `100006`，而且文案会带上权限点名
   （形如 `-[scn:purchase:view]`）——**看着像功能没开，其实是门店不对**。
   所以不给 `--shop-id` 时**不要**拿 `--shop` 顶替：那个"贴心"的回退正好踩这个坑。
4. `X-QDM-Web-Env` 的来源是 **cookie `b2b_web_env`**，不是 localStorage；`X-QDM-Api-Env`
   才来自 localStorage.apiEnv。本脚本两个头同值发出，并带上对应的 cookie。

账号密码登录（不用再手动导 token）
----------------------------------
    GET  {api_base}account/getpublickey      → {code:0, data:"<base64 公钥>"}
    POST {api_base}account/v4/b2bsignin      → {code:0, data:{token, id, shopselects, ...}}

    1. 先取公钥。服务端返回字符串 `"0"` 表示本环境不加密，那就退到明文接口
       `account/v2/b2bsignin`；否则用 **RSA(PKCS#1 v1.5)** 加密密码后打 v4。
    2. 登录请求的 body（**键顺序不能改，签名是把 body 序列化后算的**）：
         {"username":..,"password":<密文>,"rememberme":true,
          "roletype":"Shop","mobilemanagement":false}
       roletype：门店端 "Shop"、平台端 "Tenant"（前端按 fromtype 取）。
    3. 返回的 data.token 就是后续所有接口要的 B2B-Authorization。

    ⚠️ 公钥不是前端硬编码的那个。bundle 里埋的是 512 bit 的老公钥，
    线上 getpublickey 现在返回的是 **2048 bit** 的新公钥——必须用接口给的，
    硬编码那份只在接口调不通时兜底。

用法
----
    # 用账号密码换 token（密码走交互输入 / 环境变量 / 文件，不走命令行参数）
    python3 qdama_daily.py login --user 13800138000
    python3 qdama_daily.py login --user 13800138000 --role-type Shop

    # 拉某一天
    python3 qdama_daily.py fetch --date 20260923 --shop A25V --shop-id 205082 -o daily.csv

    # 拉最近 7 天（含今天）
    python3 qdama_daily.py fetch --days 7 --shop A25V --shop-id 205082 -o week.csv

    # 拉一个区间
    python3 qdama_daily.py fetch --from 20260901 --to 20260923 --shop A25V -o sep.csv

    token 按这个顺序找，不用每次都填：
      1. --token 的值
      2. --token-file 指定的文件（首行）
      3. 环境变量 QDAMA_TOKEN / QDAMA_TOKEN_FILE
      4. 约定路径 ~/.qdama_token，其次 ~/Downloads/.qdama_token

    # 只想看接口通不通（打一条，打印原始响应和用到的头）
    python3 qdama_daily.py probe --date 20260923 --shop A25V --shop-id 205082

    # 直接问服务端：这个账号有没有「每日订购」的功能权限
    python3 qdama_daily.py checkperm --perm scn:purchase:everydayexport

    # 离线自检：用真实样本验证签名实现
    python3 qdama_daily.py selftest
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime
import getpass
import hashlib
import json
import os
import random
import ssl
import string
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DAILY_LIST_PATH = "sc/purchase/order/listpurchaseordersummary"

# 权限自检接口（页面 created 时也会打这条）：POST {"permissions":[...]}。
CHECK_AUTH_PATH = "account/sysuser/check/authentication"

# 商品分类树（大 / 中 / 小三级），页面「分类」级联选择器用。GET，无参数。
CATEGORY_TREE_PATH = "product/category/getcategorytreedto"

# 主表格的列定义，**1:1 抄自页面**（chunk-51c05ef0 里 total 组件的 render 函数）。
# 每一项是 (字段名, 页面中文表头, 页面列宽)。顺序就是页面上从左到右的顺序，
# 表头文字也一字不改——GUI 要靠它把表格照着页面复刻出来。
#   注意「销售方式」页面绑的是 salesmodedesc（中文），不是数字码 salesmode。
#   注意页面表格**没有** orderno / deliveryat / createdby，那三个是弹窗里的表，
#   别顺手加进来。
PAGE_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("arrivaldate", "到店日期", 120),
    ("skucode", "商品编码", 120),
    ("skuname", "商品名称", 120),
    ("salesmodedesc", "销售方式", 120),
    ("subcategoryname", "小分类", 120),
    ("orderqty", "订购数量", 120),
    ("orderunit", "订购单位", 120),
    ("ordertype", "订单类型", 120),
    ("plantype", "处理类型", 120),
    ("orderorign", "订购来源", 120),
    ("orderstatus", "订单状态", 120),
    ("comboflag", "组合套餐", 100),
    ("remark", "备注", 150),
)

# 页面查询表单上的条件，顺序同页面。(字段名, 中文标签, 匹配方式)
#   exact  = 码值精确匹配（页面是下拉框）
#   substr = 子串包含（页面是输入框）
# 抄自页面 search() 里的 filter 实现，别凭字段名猜——skucode / skuname 看着像
# 精确匹配，实际用的是 indexOf（子串）。
FILTER_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("ordertype", "订单类型", "exact"),
    ("orderorign", "订购来源", "exact"),
    ("plantype", "处理类型", "exact"),
    ("skucode", "商品编码", "substr"),
    ("skuname", "商品名称", "substr"),
    ("salesmode", "销售方式", "exact"),
)

# 页面上那几个下拉框的选项，照抄前端 el-option 的定义。顺序一致，「全部」在最前
# （对应空字符串 = 不筛）。value 就是数据里对应字段的值，所以筛选时直接比字符串。
FILTER_OPTIONS: dict[str, tuple[str, ...]] = {
    "ordertype": ("日常订单", "紧急加单", "爆款订单", "赠品订单"),
    "orderorign": ("门店订购", "总部代订", "中台加单", "线上订单"),
    "plantype": ("门店商品订购", "电商订购", "物料订购"),
}

# 页面「销售方式」下拉的选项（app.js 里的 SALESMODE 字典）。键是数字码，
# 值是中文；筛选时比的是 salesmode 字段，但界面上显示中文。
SALES_MODE_LABELS: dict[str, str] = {
    "10": "门店", "20": "菜吧", "30": "预售", "40": "次日达",
    "50": "平台接龙", "60": "TOB", "70": "电商接龙", "99": "全渠道",
}

# 页面「分类」那个级联选择器。页面上它是一棵树（value 用分类 id/code），
# 但接口返回的每行数据里同时带着大 / 中 / 小分类的**名字**，所以这里按名字过滤：
# 不依赖分类树接口就能用，而且下拉里显示的就是人能看懂的中文。
CATEGORY_FIELDS: tuple[tuple[str, str], ...] = (
    ("bigcategoryname", "大分类"),
    ("midcategoryname", "中分类"),
    ("subcategoryname", "小分类"),
)

# 页面右上角那组「全部 / 已提交 / 已删除」按钮，筛的是 orderstatus 字段。
STATUS_TABS: tuple[str, ...] = ("全部", "已提交", "已删除")

# 登录链路。v2 = 明文密码，v4 = RSA 加密后的密码，走哪个由公钥接口决定。
LOGIN_PUBKEY_PATH = "account/getpublickey"
LOGIN_SIGNIN_PLAIN_PATH = "account/v2/b2bsignin"
LOGIN_SIGNIN_RSA_PATH = "account/v4/b2bsignin"

# checkperm 不传 --perm 时查这几个。来自每日订购页面组件的 PERMISSIONS 声明。
DEFAULT_PERMISSIONS = ["scn:purchase:everydayexport"]

# 服务端据此选择签名密钥。改这个值等于换一套密钥，签名会全错。
API_VERSION = "1.1"

# document.domain → (API 基址, 签名密钥)
ENVIRONMENTS: dict[str, tuple[str, str]] = {
    "olpadmin.qdama.cn": ("https://midoffice.qdama.cn/b2b/", "3Jr8S1K18rcC1wAfv8"),
    "olpadmin.qdama.com": ("https://midoffice.qdama.com/b2b/", "3Jr8S1K18rcC1wAfv8"),
    "olpadmin-k8s.qdama.cn": ("https://midoffice-k8s.qdama.cn/b2b/", "3Jr8S1K18rcC1wAfv8"),
    "olpadmin-k8s-2.qdama.cn": ("https://midoffice-k8s-2.qdama.cn/b2b/", "3Jr8S1K18rcC1wAfv8"),
    "olpadmin-gray01.qdama.cn": ("https://midoffice-gray01.qdama.cn/b2b/", "3Jr8S1K18rcC1wAfv8"),
    "olpadmin-gray02.qdama.cn": ("https://midoffice-gray02.qdama.cn/b2b/", "3Jr8S1K18rcC1wAfv8"),
    "olpadmin-qa2.qdama.cn": ("https://b2b-qa2.qdama.cn/b2b/", "xpMFj7Oiqx5EQK5j6J"),
    "olpadmin-qa3.qdama.cn": ("https://b2b-qa3.qdama.cn/b2b/", "xpMFj7Oiqx5EQK5j6J"),
    "olpadmin-qa-merge.qdama.cn": ("https://b2b-qa-merge.qdama.cn/b2b/", "xpMFj7Oiqx5EQK5j6J"),
    "olpadmin-dev.qdama.cn": ("https://b2b-dev.qdama.cn/b2b/", "xpMFj7Oiqx5EQK5j6J"),
    "olpadmin-dev2.qdama.cn": ("https://b2b-dev2.qdama.cn/b2b/", "xpMFj7Oiqx5EQK5j6J"),
}

DEFAULT_DOMAIN = "olpadmin.qdama.cn"
NONCE_CHARS = string.digits + string.ascii_lowercase + string.ascii_uppercase
DEFAULT_TENANT = "0210000001"
# 门店端 "Shop"、平台端 "Tenant"（前端 roletype = fromtype==1 ? "Shop" : "Tenant"）。
DEFAULT_ROLE_TYPE = "Shop"

# 兜底公钥：bundle（app.js）里按 document.domain 硬编码的那套，512 bit。
# 线上 getpublickey 已经换成 2048 bit，所以只在接口拿不到时用，且会打警告。
LEGACY_PUBKEYS: dict[str, str] = {
    "olpadmin.qdama.cn": "MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAJwAr8nfsmnOswIuQdY5hrs88aSgIKNBiyCYpgBKDX5+QYh90vG0e5iHxC0u9srRRFlpfMfDpPvQQRHADeCi6l8CAwEAAQ==",
    "olpadmin.qdama.com": "MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAN2nINiXBXIzzC6LMqS7/cyXLtEpqa+e2WcyHQoyXytWabBNRH8Vno/d/sDXCZm81LIJJwralJHYUciMMTEkqeMCAwEAAQ==",
}

# 登录成功后往哪儿写东西
DEFAULT_TOKEN_PATH = "~/Downloads/qdama-token.txt"     # 与浏览器导出同名，fetch 会自动探测
SESSION_PATH = "~/.qdama_session.json"                 # 存 shopid / sysUserId / tenant，省得每次手输
# X-QDM-Api-Env 取自 localStorage.apiEnv、X-QDM-Web-Env 取自 cookie b2b_web_env。
# 生产环境两者实测同值；换环境（灰度/qa）要跟着改，或者用 --api-env 覆盖。
DEFAULT_API_ENV = "k8s02-cluster"
DATE_FMT = "%Y%m%d"

# token 文件的约定位置：放这儿就不用每次传参
TOKEN_SEARCH_PATHS = (
    "~/.qdama_token",
    "~/Downloads/.qdama_token",
    "~/Downloads/qdama-token.txt",     # 浏览器下载不保留点开头的文件名，给个备选
)

# 服务端错误码 → 人话。撞上没见过的码就原样打出来，别硬猜。
CODE_HINTS: dict[int, str] = {
    100029: "系统签名错误——多半是请求头 v 没带，或者 nonce/参数拼错了",
    100031: "登录超时，token 失效了，重新登录再取",
    100043: "token 为空，B2B-Authorization 头没带上",
    100006: ("服务端认为这个账号没有该功能权限。**先怀疑门店头，再怀疑权限**：\n"
             "  X-QDM-Shop-Id 必须是账号有权访问的门店（登录返回 shopselects 里的），\n"
             "  填成 sapshopid、或填账号没权限的门店，都会回这个码，而且文案还带权限点名\n"
             "  （形如 -[scn:purchase:view]）——看着像功能没开，其实是门店不对。\n"
             "  两步定位：① 确认 --shop-id 用的是登录返回的门店；② 用 checkperm 看权限点\n"
             "  是否真的 available=false。顺序别反，权限点几乎总是好的。"),
    500: "服务端异常——通常是业务参数没传对（比如参数放错了位置）",
    # 登录相关（下面几条都实测过或来自登录页反编译的明确分支）
    100002: ("用户名或密码错误。⚠️ 服务端原话：连续 5 次错误该账号会被锁定——\n"
             "别反复试，先确认账号密码；密码里如果有空格或全角字符，检查是不是输入问题"),
    300030: "首次登录，服务端要求先设置新密码——去网页端登一次改了密码再来",
    300031: "首次登录流程中，同上：先去网页端把新密码设好",
    54003: "登录设备变更，服务端要求确认绑定——去网页端登录一次完成设备确认",
}


class ApiError(RuntimeError):
    """接口返回非 0 的 code，或者网络层失败。"""


# --------------------------------------------------------------------------- #
# token 与日期（命令行层的小工具）
# --------------------------------------------------------------------------- #

def resolve_token(explicit: str, token_file: str) -> tuple[str, str]:
    """找出 token，返回 (token, 来源描述)。

    优先级：--token > --token-file > 环境变量 > 约定路径自动探测。
    自动探测是为了少让用户传参——但来源会打印出来，免得静默用错文件。
    """
    if explicit:
        return explicit, "--token"

    explicit_file = token_file or os.environ.get("QDAMA_TOKEN_FILE", "")
    if explicit_file:
        path = os.path.expanduser(explicit_file)
        if not os.path.isfile(path):
            raise ApiError(f"token 文件不存在：{path}")
        candidates = [path]
    else:
        candidates = [os.path.expanduser(p) for p in TOKEN_SEARCH_PATHS]

    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError as error:
            raise ApiError(f"读不到 token 文件 {path}：{error}")
        if token:
            return token, path
    return "", "(未提供)"


def parse_date(text: str) -> datetime.date:
    try:
        return datetime.datetime.strptime(text.strip(), DATE_FMT).date()
    except ValueError:
        raise ApiError(f"日期格式不对：{text!r}，要 yyyyMMdd（例如 20260923）")


def resolve_dates(date: str, days: int | None, frm: str | None, to: str | None) -> list[str]:
    """决定要拉哪几天，返回升序的 yyyyMMdd 列表。

    优先级：--date > --days > --from/--to > 今天。
    """
    today = datetime.date.today()

    if date:
        picked = [parse_date(date)]
    elif days:
        if days < 1:
            raise ApiError("--days 至少要 1")
        if days > 366:
            raise ApiError(f"--days {days} 太多了（上限 366）")
        picked = [today - datetime.timedelta(days=i) for i in range(days - 1, -1, -1)]
    elif frm:
        start = parse_date(frm)
        end = parse_date(to) if to else today
        if end < start:
            raise ApiError(f"--to ({end:{DATE_FMT}}) 早于 --from ({start:{DATE_FMT}})")
        span = (end - start).days
        if span > 365:
            raise ApiError(f"区间 {span + 1} 天太长了（上限 366 天）")
        picked = [start + datetime.timedelta(days=i) for i in range(span + 1)]
    else:
        picked = [today]

    return [d.strftime(DATE_FMT) for d in picked]


# --------------------------------------------------------------------------- #
# 签名
# --------------------------------------------------------------------------- #

def random_nonce(length: int = 20) -> str:
    """复刻前端 randomWord(false, 20)：字符集 [0-9a-zA-Z]。"""
    return "".join(random.choice(NONCE_CHARS) for _ in range(length))


def canonical_string(params: dict[str, str], nonce: str) -> str:
    """C = query 参数 + nonce，按 key 字典序拼成 key+value+key+value…"""
    merged = {**params, "nonce": nonce}
    return "".join(k + str(merged[k]) for k in sorted(merged))


def json_body(payload: dict) -> str:
    """复刻 JS 的 JSON.stringify：无空格、不转义非 ASCII、保持插入顺序。

    这几个细节直接决定签名对不对——`{"a":1,"b":2}` 和 `{"a": 1, "b": 2}` 是两个哈希。
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def make_sign(md5key: str, c: str, timestamp: int, tail: str = "") -> str:
    return hashlib.md5(f"{md5key}{c}{timestamp}{tail}".encode()).hexdigest()


def build_request(api_base: str, path: str, body: dict | None = None,
                  query: dict | None = None, md5key: str = "", method: str = "POST",
                  timestamp: int | None = None, nonce: str | None = None,
                  ) -> tuple[str, int, str, bytes | None]:
    """完整复刻前端 main.request 的取址与载荷构造。

    前端形参顺序很反直觉——``request(url, body, callback, method, queryParams)``，
    第 2 个是 body，第 5 个才是 query。业务参数放哪会直接改签名公式：

        query 非空 → C = 把(query + nonce)按字典序拼 key+value；extra 拼在 URL 上
        query 为空 → C = "nonce" + nonce，URL 上只有 sign/nonce
        body  非空 → tail = JSON.stringify(body)，接在时间戳后面
        sign = md5(密钥 + C + P + tail)

    本接口（每日订购汇总）走的是 **POST + body**，query 为空。

    返回 ``(url, 时间戳, 方法, body 字节串或 None)``；时间戳同时要作
    `X-ZZ-Timestamp` 头发出，两边必须是同一个值。
    """
    body = dict(body or {})
    query = dict(query or {})
    nonce = nonce or random_nonce()
    stamp = int(time.time() * 1000) if timestamp is None else timestamp

    extra = ""
    if query:
        merged = {**query, "nonce": nonce}
        order = sorted(merged)
        c = "".join(part + str(merged[part]) for part in order)
        for key in order:
            if key == "nonce":
                continue
            value = merged[key]
            # 前端对名为 url 的参数单独做了 encodeURIComponent，其余原样拼
            shown = urllib.parse.quote(str(value), safe="") if key == "url" else value
            extra += f"&{key}={shown}"
    else:
        c = "nonce" + nonce

    tail = json_body(body) if body else ""
    sign = make_sign(md5key, c, stamp, tail)
    url = f"{api_base}{path}?sign={sign}&nonce={nonce}{extra}"
    data = tail.encode("utf-8") if body else None
    return url, stamp, method, data


# --------------------------------------------------------------------------- #
# RSA（登录密码加密）
# --------------------------------------------------------------------------- #

def _der_read(buf: bytes, pos: int) -> tuple[int, bytes, int]:
    """读一个 DER TLV，返回 (tag, value, 下一个字节的位置)。"""
    if pos + 2 > len(buf):
        raise ApiError("公钥 DER 数据不完整")
    tag = buf[pos]
    length = buf[pos + 1]
    pos += 2
    if length & 0x80:                      # 长形式长度
        count = length & 0x7F
        if count == 0 or pos + count > len(buf):
            raise ApiError("公钥 DER 长度字段非法")
        length = int.from_bytes(buf[pos:pos + count], "big")
        pos += count
    end = pos + length
    if end > len(buf):
        raise ApiError("公钥 DER 内容越界")
    return tag, buf[pos:end], end


def parse_rsa_public_key(der: bytes) -> tuple[int, int]:
    """从 SubjectPublicKeyInfo(DER) 里取出 RSA 的 (n, e)。

    结构固定三层：SEQUENCE { SEQUENCE{算法} , BIT STRING { SEQUENCE{ n , e } } }。
    只为加密服务，所以不追求通用——够读懂这两家的公钥就行。
    """
    tag, outer, _ = _der_read(der, 0)
    if tag != 0x30:
        raise ApiError("公钥不是 SEQUENCE，格式认不出来")
    pos = 0
    tag, _algo, pos = _der_read(outer, pos)
    if tag != 0x30:
        raise ApiError("公钥缺少算法标识")
    tag, bits, pos = _der_read(outer, pos)
    if tag != 0x03:
        raise ApiError("公钥缺少 BIT STRING")
    # BIT STRING 首字节是「未使用位数」，RSA 场景恒为 0，去掉它才是 DER 本体
    tag, inner, _ = _der_read(bits[1:], 0)
    if tag != 0x30:
        raise ApiError("RSA 公钥不是 SEQUENCE")
    pos = 0
    tag, n_bytes, pos = _der_read(inner, pos)
    if tag != 0x02:
        raise ApiError("公钥里找不到模数 n")
    tag, e_bytes, pos = _der_read(inner, pos)
    if tag != 0x02:
        raise ApiError("公钥里找不到指数 e")
    return int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")


def rsa_encrypt_pkcs1v15(public_key_b64: str, message: str) -> str:
    """复刻 JSEncrypt.encrypt(msg)：PKCS#1 v1.5 填充 + 裸 RSA + base64。

    不依赖 pycryptodome / cryptography——环境里两个解释器都没有，
    而 512~2048 bit 的裸加密用内置 pow() 就够了（模幂本来就快）。

    填充块：0x00 0x02 || 随机非零字节 || 0x00 || 明文
    """
    n, e = parse_rsa_public_key(base64.b64decode(public_key_b64))
    size = (n.bit_length() + 7) // 8
    plain = message.encode("utf-8")
    if len(plain) > size - 11:
        raise ApiError(f"密码太长：{len(plain)} 字节，这个公钥最多 {size - 11} 字节")

    pad = bytearray()
    while len(pad) < size - len(plain) - 3:
        byte = os.urandom(1)[0]
        if byte:                            # 填充字节里不允许出现 0x00
            pad.append(byte)
    block = b"\x00\x02" + bytes(pad) + b"\x00" + plain
    encrypted = pow(int.from_bytes(block, "big"), e, n)
    return base64.b64encode(encrypted.to_bytes(size, "big")).decode()


# --------------------------------------------------------------------------- #
# 登录换 token
# --------------------------------------------------------------------------- #

def fetch_public_key(domain: str = DEFAULT_DOMAIN, tenant: str = DEFAULT_TENANT,
                     token: str = "") -> str:
    """取登录加密用的公钥。返回 "" 表示服务端不想加密（它用字符串 "0" 表示）。

    这条接口**不需要登录态**（实测空 token 也返回 code 0），所以放在登录之前调。
    """
    api_base, md5key = ENVIRONMENTS[domain]
    url, stamp, method, data = build_request(
        api_base, LOGIN_PUBKEY_PATH, body={}, query={}, md5key=md5key, method="GET",
    )
    payload = call_api(url, auth_headers(token, stamp, tenant), method=method, data=data)
    if payload.get("code") != 0:
        raise ApiError(f"取公钥失败 {explain(payload)}")
    key = payload.get("data") or ""
    return "" if key == "0" else str(key)


def login(username: str, password: str, domain: str = DEFAULT_DOMAIN,
          tenant: str = DEFAULT_TENANT, role_type: str = DEFAULT_ROLE_TYPE,
          public_key: str = "", verbose: bool = True) -> dict:
    """账号密码换 token，返回服务端 data（含 token / id / shopselects / tenants）。

    形状严格照登录页的 passlogin()：POST + JSON body、query 为空。
    body 的**键顺序**必须和前端一致（username, password, rememberme, roletype,
    mobilemanagement）——签名是把 body 序列化后一起算的，顺序一变签名就废。
    """
    if not public_key:
        public_key = fetch_public_key(domain, tenant)

    api_base, md5key = ENVIRONMENTS[domain]
    if public_key:
        path = LOGIN_SIGNIN_RSA_PATH
        cipher = rsa_encrypt_pkcs1v15(public_key, password)
        key_bits = parse_rsa_public_key(base64.b64decode(public_key))[0].bit_length()
        if verbose:
            print(f"（公钥 {key_bits} bit，密码已 RSA 加密 → {len(cipher)} 字节）", file=sys.stderr)
    else:
        # 服务端没给公钥，说明这个环境走明文（前端此时打 v2 接口）
        path = LOGIN_SIGNIN_PLAIN_PATH
        cipher = password
        if verbose:
            print("（服务端未提供公钥，按前端逻辑走明文接口 account/v2/b2bsignin）",
                  file=sys.stderr)

    body = {
        "username": username,
        "password": cipher,
        "rememberme": True,
        "roletype": role_type,
        "mobilemanagement": False,          # 前端 checkIsMoible() 在桌面浏览器恒为 false
    }
    url, stamp, method, data = build_request(
        api_base, path, body=body, query={}, md5key=md5key, method="POST",
    )
    headers = auth_headers("", stamp, tenant)
    payload = call_api(url, headers, method=method, data=data)
    if payload.get("code") != 0:
        raise ApiError(f"登录失败 {explain(payload)}")
    return payload.get("data") or {}


def session_from_login(data: dict, domain: str = DEFAULT_DOMAIN) -> dict:
    """从登录返回里提炼出后续请求要用的上下文。

    **两个门店编码要分清**（实测踩过，别只存一个）：

    - ``sapshopid`` 是业务参数，决定"查哪个门店"，形如 ``A25V`` / ``A3VP``；
    - ``shopid`` 是 ``X-QDM-Shop-Id``，决定"以哪个门店的身份查"，服务端拿它
      判权限，可能是数字（``205082``）也可能是字母（``A3VP``）。

    两者不一定相等。而且 ``X-QDM-Shop-Id`` 一旦填了账号没权限的门店，服务端
    直接回 ``100006 ...-[scn:purchase:view]``——看着像功能没开，其实是门店不对。
    查门店的 SAP 码可以是任意门店，但上下文门店必须落在 ``shopselects`` 里。
    """
    shops = data.get("shopselects") or []
    tenants = data.get("tenants") or []
    normalized = [{
        "shopid": str(s.get("shopid") or ""),
        # 有些环境不给 sapshopid，那就退化成 shopid——A3VP 这类门店两者同值
        "sapshopid": str(s.get("sapshopid") or s.get("shopid") or ""),
        "shopname": s.get("shopname") or "",
    } for s in shops]
    first = normalized[0] if normalized else {}
    return {
        "domain": domain,
        "token": data.get("token") or "",
        "sys_user_id": str(data.get("id") or ""),
        "nickname": data.get("nickname") or "",
        "login_account": data.get("loginaccount") or "",
        "tenant": str((tenants[0] or {}).get("tenantid") or DEFAULT_TENANT) if tenants
                  else DEFAULT_TENANT,
        "shop_id": first.get("shopid", ""),
        "sapshopid": first.get("sapshopid", ""),
        "shops": normalized,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def save_session(session: dict) -> str:
    """把登录上下文落盘，让 fetch 不用再手输门店 / 用户 ID。"""
    path = os.path.expanduser(SESSION_PATH)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(session, handle, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)                   # 里面有 token，别让同机其他用户读
    return path


def load_session() -> dict:
    """读会话缓存；没有或读不动就返回空 dict（不打扰调用方）。"""
    path = os.path.expanduser(SESSION_PATH)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# 请求
# --------------------------------------------------------------------------- #

def auth_headers(token: str, timestamp: int, tenant: str = DEFAULT_TENANT,
                 shop_id: str = "", sys_user_id: str = "",
                 api_env: str = DEFAULT_API_ENV, web_env: str = "",
                 workbench: str = "shop") -> dict[str, str]:
    """复刻前端 main.request 的请求头。

    `v` 和 `X-ZZ-Timestamp` 是签名校验的一部分，缺一不可；`X-ZZ-Timestamp`
    必须与签名里用的那个毫秒时间戳**完全一致**。

    `X-QDM-Shop-Id` 要用 shopObj.shopid（门店内部 ID），填成 sapshopid 会被
    当成访问别的门店。前端还会带上 `shop_id` / `sys_user_id` / `b2b_web_env`
    三个 cookie，这里一并复刻，免得服务端在 cookie 里找门店上下文。
    """
    headers = {
        "v": API_VERSION,
        "X-ZZ-Timestamp": str(timestamp),
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "B2B-Authorization": token,
        "X-ZZ-TenantId": tenant or DEFAULT_TENANT,
        "X-QDM-Workbench": workbench,
    }
    if shop_id:
        headers["X-QDM-Shop-Id"] = shop_id
    if sys_user_id:
        headers["X-ZZ-SysUserId"] = sys_user_id
    if api_env:
        headers["X-QDM-Api-Env"] = api_env
    # X-QDM-Web-Env 真实来源是 cookie b2b_web_env；生产环境和 apiEnv 同值
    web_env = web_env or api_env
    if web_env:
        headers["X-QDM-Web-Env"] = web_env

    cookies = []
    if shop_id:
        cookies.append(f"shop_id={shop_id}")
    if sys_user_id:
        cookies.append(f"sys_user_id={sys_user_id}")
    if web_env:
        cookies.append(f"b2b_web_env={web_env}")
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    return headers


_SYSTEM_CA_BUNDLES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
)


def build_ssl_context() -> ssl.SSLContext:
    """构造 TLS 上下文；本机缺根证书库时兜底到系统的。

    为什么要兜底：macOS 上用官方安装包装的 Python，默认一张根证书都没有
    （实测 ``get_ca_certs()`` 返回空列表），于是**任何** HTTPS 站点都会报
    ``CERTIFICATE_VERIFY_FAILED``——看着像对方证书有问题，其实是本地没证书
    可查。系统证书库就摆在那儿，直接借。``QDAMA_CA_BUNDLE`` 可覆盖。
    """
    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context

    override = os.environ.get("QDAMA_CA_BUNDLE", "")
    candidates = ([override] if override else []) + list(_SYSTEM_CA_BUNDLES)
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            context.load_verify_locations(cafile=path)
        except (OSError, ssl.SSLError):  # pragma: no cover - 证书文件坏了才会走到
            continue
        break
    return context


def describe_network_error(error: urllib.error.URLError) -> str:
    """把网络层异常翻译成能照着做的提示，别只甩一句『连不上』。"""
    text = str(getattr(error, "reason", error))
    lowered = text.lower()
    if "certificate verify failed" in lowered:
        return ("TLS 证书校验失败：本机 Python 没有可用的根证书库，不是对方证书的问题。\n"
                "  → 脚本会自动借系统证书库（/etc/ssl/cert.pem）；仍失败就设 "
                "QDAMA_CA_BUNDLE 指向证书文件")
    if "timed out" in lowered or "timeout" in lowered:
        return f"请求超时：{text}"
    if "connection refused" in lowered:
        return f"连接被拒绝：{text}"
    if "nodename nor servname" in lowered or "name or service not known" in lowered:
        return f"域名解析失败：{text}"
    return f"连不上：{text}"


def call_api(url: str, headers: dict[str, str], method: str = "GET",
             data: bytes | None = None, timeout: float = 20.0) -> dict:
    """打一次接口。

    显式清空 ProxyHandler：环境里注入的 HTTP_PROXY 会把请求带进死胡同，
    而这个域名其实直连就通。
    """
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=build_ssl_context()),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        raise ApiError(f"HTTP {error.code}：{body[:300]}") from error
    except urllib.error.URLError as error:
        raise ApiError(describe_network_error(error)) from error
    except ssl.SSLError as error:
        raise ApiError(f"TLS 失败：{error}\n  → 试试设 QDAMA_CA_BUNDLE 指定证书文件") from error
    except json.JSONDecodeError as error:
        raise ApiError(f"响应不是 JSON：{error}") from error


def explain(payload: dict) -> str:
    code = payload.get("code")
    message = payload.get("message") or ""
    hint = CODE_HINTS.get(code if isinstance(code, int) else -1)
    return f"code={code} {message}".strip() + (f"\n  → {hint}" if hint else "")


def fetch_daily_page(date: str, sapshopid: str, token: str,
                     domain: str = DEFAULT_DOMAIN, tenant: str = DEFAULT_TENANT,
                     shop_id: str = "", sys_user_id: str = "",
                     api_env: str = DEFAULT_API_ENV) -> dict:
    """取某天的日常订购**完整返回**（明细 + 页面底部那两个合计）。date 用 yyyyMMdd。

    形状必须和页面一致：POST + JSON body，query 为空（见模块头注释第 2 条坑）。
    接口一次返回当天全量，页面上那些查询条件都是前端本地过滤的——所以这里
    只能按「日 + 门店」取，没有分页参数可用。

    返回的 dict 里，列表是 ``purchaseordersummarylist``；合计是 ``totalskuqty``
    （合计品项，按商品编码去重后的个数）和 ``totalorderqty``（订购数量之和）。
    """
    api_base, md5key = ENVIRONMENTS[domain]
    url, stamp, method, data = build_request(
        api_base, DAILY_LIST_PATH,
        body={"arrivaldate": date, "sapshopid": sapshopid},
        md5key=md5key, method="POST",
    )
    headers = auth_headers(token, stamp, tenant, shop_id or sapshopid, sys_user_id, api_env)
    payload = call_api(url, headers, method=method, data=data)

    if payload.get("code") != 0:
        raise ApiError(explain(payload))

    return payload.get("data") or {}


def fetch_daily_summary(date: str, sapshopid: str, token: str,
                        domain: str = DEFAULT_DOMAIN, tenant: str = DEFAULT_TENANT,
                        shop_id: str = "", sys_user_id: str = "",
                        api_env: str = DEFAULT_API_ENV) -> list[dict]:
    """取某天的日常订购明细，只要列表。要合计值就用 fetch_daily_page。"""
    part = fetch_daily_page(date, sapshopid, token, domain, tenant,
                            shop_id, sys_user_id, api_env)
    return part.get("purchaseordersummarylist") or []


def fetch_category_tree(token: str, domain: str = DEFAULT_DOMAIN,
                        tenant: str = DEFAULT_TENANT, shop_id: str = "",
                        sys_user_id: str = "",
                        api_env: str = DEFAULT_API_ENV) -> list[dict]:
    """商品分类树（大 / 中 / 小三级），页面上「分类」那个级联选择器的数据源。

    页面在 created 里就打这条，是个 GET、不带业务参数。返回结构是嵌套的
    分类节点，字段名以服务端为准——GUI 里只做容错解析，不假设固定层级。
    """
    api_base, md5key = ENVIRONMENTS[domain]
    url, stamp, method, data = build_request(
        api_base, CATEGORY_TREE_PATH, md5key=md5key, method="GET",
    )
    headers = auth_headers(token, stamp, tenant, shop_id, sys_user_id, api_env)
    payload = call_api(url, headers, method=method, data=data)
    if payload.get("code") != 0:
        raise ApiError(explain(payload))
    return payload.get("data") or []


# --------------------------------------------------------------------------- #
# 页面级的筛选 / 分页 / 合计
#
# 「日常订购」页面的查询条件**不是发给服务端**的：接口只收 日期 + 门店，一次返回
# 当天全量；页面上那些下拉框和输入框，是在浏览器里对这批数据做本地过滤。
# 下面这几个函数就是那段前端逻辑的等价实现——GUI 靠它们把页面行为复刻出来，
# 顺带也让它变成可离线测试的纯函数（前端那份没法测）。
# --------------------------------------------------------------------------- #

def filter_rows(rows: list[dict], params: dict) -> list[dict]:
    """按查询条件过滤。``params`` 的键名与页面一致，空值 / 缺省表示该项不筛。

    匹配方式照抄页面 search() 里的 filter：
      - 码值字段（订单类型 / 订购来源 / 处理类型 / 销售方式 / 三级分类）精确相等；
      - 商品编码、商品名称用的是 ``indexOf``，也就是**子串包含**，不是精确匹配。
        这两个看着像精确匹配，实际不是——照字段名猜会做错。
    """
    rules = FILTER_FIELDS + tuple(
        (field, label, "exact") for field, label in CATEGORY_FIELDS
    )
    kept = []
    for row in rows:
        for field, _label, mode in rules:
            wanted = str(params.get(field) or "")
            if not wanted:
                continue
            actual = str(row.get(field) or "")
            hit = (actual == wanted) if mode == "exact" else (wanted in actual)
            if not hit:
                break
        else:
            kept.append(row)
    return kept


def filter_by_status(rows: list[dict], status: str) -> list[dict]:
    """页面右上角那组「全部 / 已提交 / 已删除」按 ``orderstatus`` 过滤。

    页面用空字符串表示「全部」，这里额外接受中文「全部」——叫得明白点。
    """
    wanted = "" if status in ("", "全部") else status
    if not wanted:
        return list(rows)
    return [row for row in rows if str(row.get("orderstatus") or "") == wanted]


def paginate(rows: list[dict], page: int, size: int) -> list[dict]:
    """客户端分页，同页面 handlePageData 的 slice。"""
    if size <= 0:
        return list(rows)
    start = max(0, (max(1, page) - 1) * size)
    return list(rows[start:start + size])


def summarize(rows: list[dict]) -> dict:
    """页面底部那两个合计。

    - ``totalskuqty``  合计品项：商品编码**去重**后的个数（同页面 new Set().length，
      所以一个空编码也会被算成一项，保持和页面一致）。
    - ``totalorderqty`` 订购数量之和，保留 2 位小数。数量为空或 0 的行不计入
      （页面那句 ``filter(t => t.orderqty)`` 就是这个意思），别把它当成 bug。
    """
    codes = {str(row.get("skucode") or "") for row in rows}
    total = 0.0
    for row in rows:
        raw = row.get("orderqty")
        if not raw:
            continue
        try:
            total += float(raw)
        except (TypeError, ValueError):
            continue
    return {"totalskuqty": len(codes), "totalorderqty": f"{total:.2f}"}


def check_permission(token: str, permissions: list[str],
                     domain: str = DEFAULT_DOMAIN, tenant: str = DEFAULT_TENANT,
                     shop_id: str = "", sys_user_id: str = "",
                     api_env: str = DEFAULT_API_ENV) -> list[dict]:
    """问服务端：这些权限点这个账号开没开。

    页面自己在 created 里也打这条（checkauthentication）。100006 到底是
    "真没权限"还是"请求形状不对"，用它一句话就能分辨。
    """
    api_base, md5key = ENVIRONMENTS[domain]
    url, stamp, method, data = build_request(
        api_base, CHECK_AUTH_PATH,
        body={"permissions": list(permissions)},
        md5key=md5key, method="POST",
    )
    headers = auth_headers(token, stamp, tenant, shop_id, sys_user_id, api_env)
    payload = call_api(url, headers, method=method, data=data)
    if payload.get("code") != 0:
        raise ApiError(explain(payload))
    return payload.get("data") or []


# --------------------------------------------------------------------------- #
# 离线校准（环境升级把密钥换掉时，用它重新反推）
# --------------------------------------------------------------------------- #

def signature_variants(params: dict[str, str], nonce: str):
    """枚举候选的 (标签, C, 尾部)。

    通式已经确认了（C=字典序拼接、无尾部），但换个环境或版本可能变，
    所以留一条穷举路径给 calibrate 用。
    """
    merged = {**params, "nonce": nonce}
    ordered = [(k, merged[k]) for k in sorted(merged)]
    kv = "".join(k + str(v) for k, v in ordered)

    c_forms = {
        "sorted_all": kv,
        "sorted_wo_nonce": "".join(k + str(v) for k, v in ordered if k != "nonce"),
        "raw_all": "".join(k + str(v) for k, v in merged.items()),
        "nonce_only": "nonce" + nonce,
    }
    tails = {
        "none": "",
        "json_with_nonce": json.dumps(merged, separators=(",", ":")),
        "json_plain": json.dumps(params, separators=(",", ":")),
        "form": urllib.parse.urlencode(merged),
    }
    for c_name, c in c_forms.items():
        for tail_name, tail in tails.items():
            yield f"C={c_name} tail={tail_name}", c, tail


def calibrate(real_url: str, md5key: str | None = None, window: float = 600.0,
              verbose: bool = True) -> tuple[str, int]:
    """拿一条真实请求 URL，本地穷举出签名拼法与当时的时间戳。

    全程不发网络请求——纯离线计算。拿生产接口试签名既慢又留痕。
    """
    if md5key is None:
        _, md5key = detect_environment(real_url)

    query = urllib.parse.parse_qs(urllib.parse.urlparse(real_url).query, keep_blank_values=True)
    sign = query.pop("sign", [None])[0]
    nonce = query.pop("nonce", [None])[0]
    if not sign or not nonce:
        raise ApiError(
            "这段 URL 里没有 sign / nonce。要的是接口请求（XHR），不是页面地址。\n"
            "页面地址长这样：https://olpadmin.qdama.cn/#/dailySummary"
        )

    params = {k: v[0] for k, v in query.items()}
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(window * 1000)
    md5 = hashlib.md5

    # 时间戳字节串对所有候选都一样，先算一次；内层要跑几十万次，省掉重复编码。
    stamps_ms = [str(ts).encode() for ts in range(start_ms, now_ms + 1)]
    stamps_sec = [str(ts).encode() for ts in range(start_ms // 1000, now_ms // 1000 + 1)]

    count = 0
    if verbose:
        print(f"离线扫描中：窗口 {window:.0f} 秒，参数 {params}")

    for label, c, tail in signature_variants(params, nonce):
        count += 1
        prefix = f"{md5key}{c}".encode()
        tail_bytes = tail.encode()
        for unit, stamps, base in (("ms", stamps_ms, start_ms),
                                   ("sec", stamps_sec, start_ms // 1000)):
            for index, stamp in enumerate(stamps):
                if md5(prefix + stamp + tail_bytes).hexdigest() == sign:
                    if verbose:
                        print(f"命中（试了 {count} 种拼法）")
                    tagged = label if unit == "ms" else f"{label}（秒级时间戳）"
                    return tagged, base + index

    raise ApiError(
        f"扫了 {count} 种拼法都没对上。可能原因：\n"
        "  · 这条请求不是当前环境发的（各环境密钥不同）\n"
        "  · 服务端换了密钥版本（请求头 v 的值变了）\n"
        "  · 时间戳超出扫描窗口——用 --window 调大（默认 600 秒）"
    )


def detect_environment(url: str) -> tuple[str, str]:
    """从 URL 认出环境，返回 (API 基址, 密钥)。

    必须先精确匹配 host：字典里 "olpadmin.qdama.cn" 是 "olpadmin-qa2.qdama.cn"
    的子串，用 contains 一把梭会把 qa 认成生产，而两者密钥不同。
    """
    host = urllib.parse.urlparse(url).hostname or ""

    if host in ENVIRONMENTS:
        return ENVIRONMENTS[host]

    hits = [domain for domain in ENVIRONMENTS if domain in host]
    if hits:
        return ENVIRONMENTS[max(hits, key=len)]

    for api, key in ENVIRONMENTS.values():
        if urllib.parse.urlparse(api).hostname == host:
            return api, key

    return ENVIRONMENTS[DEFAULT_DOMAIN]


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

# 三条从浏览器真实抓到 / 反编译对齐的样本，用来锁死签名实现。
# A、B 是 query 里带业务参数的老形状，两次独立抓包可交叉验证算法本身。
REAL_SAMPLE = {
    "params": {"arrivaldate": "20260923", "sapshopid": "A25V"},
    "nonce": "KIop3DQ1UBwMLE9myRzI",
    "timestamp": 1790140163050,
    "sign": "b037721e08b2ead45f191dc0e0f85f01",
}
REAL_SAMPLE_2 = {
    "params": {"arrivaldate": "20260923", "sapshopid": "A25V"},
    "nonce": "TY9lDofMJE9k023UtdfH",
    "timestamp": 1790141461282,
    "sign": "6b6ff67de6aa0bedc5306005fcc6600c",
}
# C 是本接口在页面里的真实形状：POST + JSON body，query 只有 sign/nonce。
# 期望值 = md5(密钥 + "nonce"+nonce + P + '{"arrivaldate":"20260923","sapshopid":"A25V"}')，
# 手推得到——它同时锁住了「C 退化成 nonce+nonce」和「JSON 序列化不带空格」两件事。
REAL_SAMPLE_POST = {
    "body": {"arrivaldate": "20260923", "sapshopid": "A25V"},
    "nonce": "TY9lDofMJE9k023UtdfH",
    "timestamp": 1790141461282,
    "sign": "6eec83365e2c3901bf016ed1f88e25f3",
}

# 自检专用的固定 RSA 密钥对（512 bit，开发时现生成，只用来验证加密实现）。
# 把私钥写在这儿没有任何风险——它不保护任何真实数据，唯一用途是让自检
# 能把刚加出来的密文当场解回去，确认 PKCS#1 v1.5 的填充结构没错。
TEST_RSA_PUBKEY = ("MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAJ/mFzkGi+PtpYWyut004Fx3+wycRjpA"
                   "WvrU6hfZ1W8bo/48rclR/+AhbAdLJTjAEqB99fMBBdfHfctkSE4Pu/cCAwEAAQ==")
TEST_RSA_N = int.from_bytes(base64.b64decode(
    "n+YXOQaL4+2lhbK63TTgXHf7DJxGOkBa+tTqF9nVbxuj/jytyVH/4CFsB0slOMASoH318wEF18d9y2RITg+79w=="
), "big")
TEST_RSA_D = int.from_bytes(base64.b64decode(
    "jHhwklJj9rrBnPDlJIvdRp7I1806DNaYlp8RgB6IWHBGXF9dhx52E/BOzgR0UNbScGXCogIpaKfnbBqLonWScQ=="
), "big")

# 线上 getpublickey 实抓到的那把公钥（2048 bit）——确认解析器吃得下真实数据。
REAL_PUBKEY_2048 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAu/roqx9hrZCKih5+sanZ6SVxUh+IJnQKtsz"
    "Azq73EkqURpjK4RqUX7ZhnqSOKINQ1lwK6M2XiTYwx/AKWcF4C4YDwVtWVzFDZTZsT0bjZmKAPq6/u"
    "VD2aV8ydhDVBVTIN8uM6QBTywIynKzQke2qpBejnLp0uqdnsnAsJvXc2iE9Rn7ET6gPAlAXl3GzOD+"
    "a5BTCaet59qC4jiWo8i54JaZddYSgOM1Z72FicfglxL3+jcC5U3xizPwaSarDQUV6H5mlH78sRJ6vri"
    "AQjLTsWozHjexxB2o4Fk7igQJO11qQb8SIISKIUNDkO3oN145uCr0LLUR/g0F6L5oxb25DDwIDAQAB")


def selftest() -> int:
    """用真实样本验证签名实现。不发任何网络请求。"""
    key = ENVIRONMENTS[DEFAULT_DOMAIN][1]
    failures = 0

    for name, sample in (("真实样本 A（query 带业务参数）", REAL_SAMPLE),
                         ("真实样本 B（同接口另一时刻，交叉验证）", REAL_SAMPLE_2),
                         ("真实样本 C（页面真实形状：POST + body）", REAL_SAMPLE_POST)):
        params = sample.get("params") or {}
        body = sample.get("body") or {}
        c = canonical_string(params, sample["nonce"]) if params else "nonce" + sample["nonce"]
        tail = json_body(body) if body else ""
        got = make_sign(key, c, sample["timestamp"], tail)
        ok = got == sample["sign"]
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {name}\n      got      {got}\n      expected {sample['sign']}")

    # query 非空时不应拼出尾串（那会跑到别的分支上去）
    got = make_sign(key, canonical_string(REAL_SAMPLE["params"], REAL_SAMPLE["nonce"]),
                    REAL_SAMPLE["timestamp"])
    ok = got == REAL_SAMPLE["sign"]
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} query 分支不带 JSON 尾串 → {got}")

    # JSON 序列化必须和 JS 的 JSON.stringify 逐字节一致：无空格、不转义中文
    got = json_body({"a": "东莞地标广场", "n": 1})
    ok = got == '{"a":"东莞地标广场","n":1}'
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} body 序列化同 JSON.stringify → {got}")

    # 本接口的请求形状：POST、body 带上业务参数、URL 里只有 sign/nonce
    api_base, md5key = ENVIRONMENTS[DEFAULT_DOMAIN]
    url, stamp, method, data = build_request(
        api_base, DAILY_LIST_PATH, body=REAL_SAMPLE_POST["body"], md5key=md5key,
        method="POST", timestamp=REAL_SAMPLE_POST["timestamp"],
        nonce=REAL_SAMPLE_POST["nonce"],
    )
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    ok = (method == "POST"
          and parsed.path.endswith(DAILY_LIST_PATH)
          and sorted(query) == ["nonce", "sign"]
          and query["sign"] == [REAL_SAMPLE_POST["sign"]]
          and query["nonce"] == [REAL_SAMPLE_POST["nonce"]]
          and data == json_body(REAL_SAMPLE_POST["body"]).encode()
          and stamp == REAL_SAMPLE_POST["timestamp"])
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 请求形状：POST + body（URL 只带 sign/nonce）")
    print(f"      {method} {parsed.path}?{parsed.query}")
    print(f"      body {data.decode() if data else None}")

    # 请求头：v / X-ZZ-Timestamp 是签名校验的一部分，门店 cookie 也要跟着走
    headers = auth_headers("TOK", REAL_SAMPLE_POST["timestamp"],
                           shop_id="205082", sys_user_id="136387")
    ok = (headers.get("v") == API_VERSION
          and headers.get("X-ZZ-Timestamp") == str(REAL_SAMPLE_POST["timestamp"])
          and headers.get("X-QDM-Shop-Id") == "205082"
          and headers.get("X-ZZ-SysUserId") == "136387"
          and "shop_id=205082" in headers.get("Cookie", "")
          and "sys_user_id=136387" in headers.get("Cookie", ""))
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 请求头与 cookie（v / 时间戳 / 门店 / 用户）")

    # nonce 字符集与长度要跟前端一致
    n = random_nonce()
    ok = len(n) == 20 and all(ch in NONCE_CHARS for ch in n)
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} nonce 生成（20 位 [0-9a-zA-Z]）→ {n}")

    # 时间戳必须是毫秒级
    now = int(time.time() * 1000)
    ok = 1_600_000_000_000 < now < 2_000_000_000_000
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 时间戳为毫秒级 → {now}")

    # ---- 登录：RSA 公钥解析 ----
    n, e = parse_rsa_public_key(base64.b64decode(TEST_RSA_PUBKEY))
    ok = n == TEST_RSA_N and e == 65537
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 公钥解析（测试密钥）→ {n.bit_length()} bit, e={e}")

    n_real, e_real = parse_rsa_public_key(base64.b64decode(REAL_PUBKEY_2048))
    ok = n_real.bit_length() == 2048 and e_real == 65537
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 公钥解析（线上实抓的公钥）→ "
          f"{n_real.bit_length()} bit, e={e_real}")

    legacy = LEGACY_PUBKEYS[DEFAULT_DOMAIN]
    n_legacy, _ = parse_rsa_public_key(base64.b64decode(legacy))
    ok = n_legacy.bit_length() == 512
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 公钥解析（bundle 里硬编码的兜底公钥）→ "
          f"{n_legacy.bit_length()} bit")

    # ---- 登录：PKCS#1 v1.5 加密后用私钥解回来，验证填充结构 ----
    # 加密本身是随机的（填充字节随机），所以只能靠「解回来对不对」来验。
    plain = "Aa1!密码-2026".encode("utf-8")
    size = (TEST_RSA_N.bit_length() + 7) // 8
    cipher_b64 = rsa_encrypt_pkcs1v15(TEST_RSA_PUBKEY, plain.decode("utf-8"))
    cipher = base64.b64decode(cipher_b64)
    block = pow(int.from_bytes(cipher, "big"), TEST_RSA_D, TEST_RSA_N).to_bytes(size, "big")
    pad_len = size - len(plain) - 3
    ok = (len(cipher) == size
          and block[:2] == b"\x00\x02"
          and all(b != 0 for b in block[2:2 + pad_len])
          and block[2 + pad_len] == 0
          and block[3 + pad_len:] == plain)
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} PKCS#1 v1.5 加密往返（含中文）→ "
          f"{len(cipher)} 字节密文，解密回 {block[3 + pad_len:].decode('utf-8')!r}")

    # 明文超过 k-11 必须报错，而不是默默产出错数据
    try:
        rsa_encrypt_pkcs1v15(TEST_RSA_PUBKEY, "x" * (size - 10))
        ok = False
    except ApiError:
        ok = True
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 超长密码会报错（上限 k-11 = {size - 11} 字节）")

    # ---- 登录：body 键顺序 + 请求形状 ----
    # 签名是把 body 序列化后一起算的，键顺序 / 布尔写法一变签名就废，必须锁死。
    login_body = {
        "username": "u",
        "password": "p",
        "rememberme": True,
        "roletype": "Shop",
        "mobilemanagement": False,
    }
    got = json_body(login_body)
    expected = ('{"username":"u","password":"p","rememberme":true,'
                '"roletype":"Shop","mobilemanagement":false}')
    ok = got == expected
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 登录 body 序列化 → {got}")

    api_base, md5key = ENVIRONMENTS[DEFAULT_DOMAIN]
    url, stamp, method, data = build_request(
        api_base, LOGIN_SIGNIN_RSA_PATH, body=login_body, query={}, md5key=md5key,
        method="POST", timestamp=REAL_SAMPLE_POST["timestamp"],
        nonce=REAL_SAMPLE_POST["nonce"],
    )
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    ok = (method == "POST"
          and parsed.path.endswith(LOGIN_SIGNIN_RSA_PATH)
          and sorted(query) == ["nonce", "sign"]
          and data == expected.encode())
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 登录请求形状：POST {LOGIN_SIGNIN_RSA_PATH}，"
          f"URL 只带 sign/nonce")

    # 公钥接口是 GET、body 为空，签名该走「无尾串」那条分支
    url, stamp, method, data = build_request(
        api_base, LOGIN_PUBKEY_PATH, body={}, query={}, md5key=md5key, method="GET",
    )
    ok = method == "GET" and data is None and "sign=" in url
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 公钥请求形状：GET {LOGIN_PUBKEY_PATH}，无 body")

    # TLS 兜底：macOS 官方包装的 Python 默认没有根证书，必须能借到系统那份
    context = build_ssl_context()
    ca_count = len(context.get_ca_certs())
    ok = ca_count > 0
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} TLS 根证书可用（{ca_count} 张）")

    # 日期解析：单天 / 最近 N 天 / 区间
    today = datetime.date.today().strftime(DATE_FMT)
    checks = [
        ("日期：单天", resolve_dates("20260923", None, "", ""), ["20260923"]),
        ("日期：区间（闭区间）",
         resolve_dates("", None, "20260901", "20260903"),
         ["20260901", "20260902", "20260903"]),
    ]
    for name, got, expected in checks:
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {name} → {got}")

    week = resolve_dates("", 3, "", "")
    ok = len(week) == 3 and week[-1] == today and week == sorted(week)
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 日期：最近 3 天（升序、含今天）→ {week}")

    ok = resolve_dates("", None, "", "") == [today]
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 日期：什么都不给 → 今天 {today}")

    try:
        resolve_dates("", None, "20260903", "20260901")
        ok = False
    except ApiError:
        ok = True
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 日期：--to 早于 --from 会报错")

    # token 解析：文件优先于环境变量，显式 --token 优先于文件
    with tempfile.TemporaryDirectory() as tmp:
        token_path = os.path.join(tmp, "tok")
        with open(token_path, "w", encoding="utf-8") as handle:
            handle.write("  TOK-FROM-FILE\n")
        got, source = resolve_token("", token_path)
        ok = got == "TOK-FROM-FILE"
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} token：读文件并去掉首尾空白 → {got!r}")

        got, source = resolve_token("TOK-EXPLICIT", token_path)
        ok = got == "TOK-EXPLICIT" and source == "--token"
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} token：--token 优先于文件 → {got!r}")

        try:
            resolve_token("", os.path.join(tmp, "nope"))
            ok = False
        except ApiError:
            ok = True
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} token：指定的文件不存在会报错")

    # 多天拉取时列取并集，后面的字段不能被丢
    got = _union_columns([{"__date": "20260923", "a": 1}, {"a": 2, "b": 3}])
    ok = got == ["__date", "a", "b"]
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 输出列取并集 → {got}")

    # ---------------------------------------------------------------- 页面复刻
    # 列定义是「照页面复刻」这个承诺的锚点：顺序、中文表头、绑定的字段名，
    # 改动都该被看见——GUI 直接吃这份定义。
    ok = (len(PAGE_COLUMNS) == 13
          and PAGE_COLUMNS[0][:2] == ("arrivaldate", "到店日期")
          and PAGE_COLUMNS[-1][:2] == ("remark", "备注"))
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 页面列定义：{len(PAGE_COLUMNS)} 列，"
          f"首「{PAGE_COLUMNS[0][1]}」末「{PAGE_COLUMNS[-1][1]}」")

    ok = ("salesmodedesc", "销售方式", 120) in PAGE_COLUMNS
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 列定义：销售方式绑 salesmodedesc（中文）而非 salesmode（码值）")

    sample = [
        {"skucode": "1001", "skuname": "鲜牛奶180ml", "ordertype": "日常订单",
         "orderqty": "4.000", "orderstatus": "已提交", "subcategoryname": "鲜奶类"},
        {"skucode": "2002", "skuname": "鸡蛋30枚", "ordertype": "紧急加单",
         "orderqty": "0", "orderstatus": "已删除", "subcategoryname": "鸡蛋类"},
        {"skucode": "1003", "skuname": "酸奶", "ordertype": "日常订单",
         "orderqty": "1.500", "orderstatus": "已提交", "subcategoryname": "鲜奶类"},
    ]

    ok = len(filter_rows(sample, {"ordertype": "日常订单"})) == 2
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 筛选：码值字段精确匹配（订单类型=日常订单 → 2 行）")

    ok = len(filter_rows(sample, {"skucode": "100"})) == 2
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 筛选：商品编码是子串匹配（'100' → 2 行，精确匹配会是 0）")

    ok = len(filter_rows(sample, {"skuname": "奶"})) == 2
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 筛选：商品名称是子串匹配（'奶' → 2 行）")

    ok = len(filter_rows(sample, {"subcategoryname": "鲜奶类"})) == 2
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 筛选：分类按名字精确匹配（鲜奶类 → 2 行）")

    ok = len(filter_rows(sample, {"ordertype": "", "skuname": ""})) == 3
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 筛选：条件为空表示不筛 → 3 行")

    ok = (len(filter_rows(sample, {"ordertype": "日常订单", "subcategoryname": "鸡蛋类"})) == 0)
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 筛选：多个条件是与关系（互相矛盾 → 0 行）")

    ok = (len(filter_by_status(sample, "已提交")) == 2
          and len(filter_by_status(sample, "已删除")) == 1
          and len(filter_by_status(sample, "全部")) == 3)
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 状态切换：全部 3 / 已提交 2 / 已删除 1")

    ok = (len(paginate(sample, 1, 2)) == 2 and len(paginate(sample, 2, 2)) == 1
          and len(paginate(sample, 9, 2)) == 0)
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 分页：每页 2 条 → 第 1 页 2 行、第 2 页 1 行、越界 0 行")

    got = summarize(sample)
    ok = got == {"totalskuqty": 3, "totalorderqty": "5.50"}
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'} 合计：品项去重 3、数量 4.000+1.500=5.50（0 不计入）→ {got}")

    print(f"\n{'全部通过' if not failures else f'{failures} 项失败'}")
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #

def _union_columns(rows: list[dict]) -> list[str]:
    """列取并集，保持首次出现的顺序。

    多天拉取时，某天多出来的字段不会因为「第一行没有」就把整列丢掉。
    """
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns


def _write_rows(rows: list[dict], out_path: str | None) -> None:
    if not rows:
        print("没有数据。")
        return
    columns = _union_columns(rows)
    if out_path:
        with open(out_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        print(f"已写入 {len(rows)} 行 → {out_path}")
    else:
        width = {c: max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
        print("  ".join(str(c).ljust(width[c]) for c in columns))
        for row in rows:
            print("  ".join(str(row.get(c, "")).ljust(width[c]) for c in columns))
        print(f"\n共 {len(rows)} 行，{len(columns)} 列")


def _add_auth(parser: argparse.ArgumentParser) -> None:
    """认证 / 环境相关的通用参数。checkperm 用不上门店和日期，但用得上这些。

    门店 / 用户 / 租户的默认值优先取上次 `login` 存下的会话缓存（~/.qdama_session.json），
    这样登录过之后跑 fetch 只要给 --shop 就够了。
    """
    session = load_session()
    parser.add_argument("--token", default=os.environ.get("QDAMA_TOKEN", ""),
                        help="登录 token，默认读环境变量 QDAMA_TOKEN")
    parser.add_argument("--token-file", default=os.environ.get("QDAMA_TOKEN_FILE", ""),
                        help="从文件首行读 token；比 --token 干净，不会进命令历史")
    parser.add_argument("--tenant", default=session.get("tenant") or DEFAULT_TENANT,
                        help=f"租户 ID，默认 {DEFAULT_TENANT}（login 之后自动带上）")
    parser.add_argument("--sys-user-id", default=session.get("sys_user_id") or "",
                        help="sysUserId；login 之后自动带上")
    parser.add_argument("--shop-id", default=session.get("shop_id") or "",
                        help="shopObj.shopid（如 205082），用于 X-QDM-Shop-Id 头；"
                             "login 之后自动带上。不给我就用 --shop 顶替，可能被判越权")
    parser.add_argument("--api-env", default=DEFAULT_API_ENV,
                        help=f"localStorage.apiEnv，默认 {DEFAULT_API_ENV}；灰度/qa 环境要改")
    parser.add_argument("--domain", default=session.get("domain") or DEFAULT_DOMAIN,
                        choices=sorted(ENVIRONMENTS))


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", default="", help="到货日期，yyyyMMdd，例如 20260923")
    parser.add_argument("--days", type=int, default=None, help="最近 N 天（含今天）")
    parser.add_argument("--from", dest="frm", default="", help="区间起始日期 yyyyMMdd")
    parser.add_argument("--to", default="", help="区间结束日期 yyyyMMdd，默认今天")
    parser.add_argument("--shop", required=True, help="门店 SAP 编码（sapshopid）")
    _add_auth(parser)
    parser.add_argument("--sleep", type=float, default=0.3,
                        help="多天拉取时每天之间的间隔秒数，默认 0.3")


def _read_password(password_file: str) -> str:
    """密码来源：--password-file > 环境变量 QDAMA_PASSWORD > 交互输入。

    刻意**不提供** `--password`：命令行参数会留在 shell 历史里，也能被同机的
    `ps` 看到。交互输入用 getpass，不回显、不落盘。
    """
    if password_file:
        path = os.path.expanduser(password_file)
        if not os.path.isfile(path):
            raise ApiError(f"密码文件不存在：{path}")
        with open(path, encoding="utf-8") as handle:
            password = handle.readline().strip()
        if not password:
            raise ApiError(f"密码文件是空的：{path}")
        return password

    from_env = os.environ.get("QDAMA_PASSWORD", "")
    if from_env:
        return from_env

    if not sys.stdin.isatty():
        raise ApiError("需要密码，但当前不是交互终端。\n"
                       "  → 用 --password-file <文件>，或设置 QDAMA_PASSWORD 环境变量")
    password = getpass.getpass("登录密码（不回显）：")
    if not password:
        raise ApiError("密码为空，取消了")
    return password


def run_login(args) -> int:
    password = _read_password(args.password_file)
    data = login(args.user, password, domain=args.domain, tenant=args.tenant,
                 role_type=args.role_type)
    token = data.get("token") or ""
    if not token:
        raise ApiError(f"登录返回里没有 token：{json_body(data)[:200]}")

    who = " ".join(x for x in (data.get("nickname"), data.get("loginaccount")) if x)
    print(f"✓ 登录成功 {who or args.user}")
    print(f"  token      {len(token)} 字符（不打印内容）")
    print(f"  sysUserId  {data.get('id') or '(无)'}")

    shops = data.get("shopselects") or []
    for index, shop in enumerate(shops):
        tail = "   ← fetch 默认用这个作 X-QDM-Shop-Id" if index == 0 else ""
        print(f"  门店       {shop.get('shopid')}  {shop.get('shopname') or ''}{tail}")
    if not shops:
        print("  门店       服务端没返回 shopselects，拉数据时用 --shop 指定 SAP 码")

    session = session_from_login(data, args.domain)

    if args.no_save:
        print("\n--no-save：没有写任何文件。")
    else:
        token_path = os.path.expanduser(args.save_token)
        with open(token_path, "w", encoding="utf-8") as handle:
            handle.write(token)
        os.chmod(token_path, 0o600)
        print(f"\ntoken 已写入 {token_path}")
        print(f"会话上下文（门店 / 用户 / 租户）已写入 {save_session(session)}")

    # 存完顺手验一次：token 拿到了却不能用，越早知道越好
    try:
        check_permission(token, DEFAULT_PERMISSIONS, domain=args.domain,
                         tenant=session["tenant"], shop_id=session["shop_id"],
                         sys_user_id=session["sys_user_id"])
        print("✓ token 实测可用（已通过一个需要登录态的接口）")
    except ApiError as error:
        print(f"⚠ token 自检没通过：{error}", file=sys.stderr)
        print("  token 已写盘，但看起来不能直接用——把上面这条发我", file=sys.stderr)

    if not args.no_save:
        # 用 __file__ 而不是 sys.argv[0]：从 stdin 跑时后者是 "-"，打印出来没法照抄
        script = os.path.basename(globals().get("__file__") or "qdama_daily.py")
        print("\n接下来直接拉数据就行，token 和门店 ID 都会自动带上：")
        print(f"  python3 {script} fetch --days 7 --shop <SAP码> -o ~/Downloads/daily.csv")

    if args.show_json:
        shown = dict(data)
        shown["token"] = f"<{len(token)} 字符，已省略>"
        print("\n服务端返回（token 已脱敏）：")
        print(json.dumps(shown, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="钱大妈智慧中台 · 日常订购数据导出",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    log = sub.add_parser("login", help="用账号密码换 token，并写入约定路径（之后 fetch 免参数）")
    log.add_argument("--user", required=True, help="登录账号（手机号 / 工号）")
    log.add_argument("--password-file", default="",
                     help="从文件首行读密码；不给就交互输入（推荐），或用环境变量 QDAMA_PASSWORD")
    log.add_argument("--role-type", default=DEFAULT_ROLE_TYPE, choices=["Shop", "Tenant"],
                     help=f"门店端 Shop / 平台端 Tenant，默认 {DEFAULT_ROLE_TYPE}")
    log.add_argument("--save-token", default=DEFAULT_TOKEN_PATH,
                     help=f"token 写到哪个文件，默认 {DEFAULT_TOKEN_PATH}（fetch 会自动探测）")
    log.add_argument("--no-save", action="store_true", help="只验证能不能登录，不写任何文件")
    log.add_argument("--show-json", action="store_true",
                     help="打印服务端返回的完整 data（token 自动脱敏）")
    log.add_argument("--tenant", default=DEFAULT_TENANT, help=f"租户 ID，默认 {DEFAULT_TENANT}")
    log.add_argument("--domain", default=DEFAULT_DOMAIN, choices=sorted(ENVIRONMENTS))

    fet = sub.add_parser("fetch", help="拉取日常订购数据（单天 / 最近 N 天 / 区间）")
    _add_common(fet)
    fet.add_argument("-o", "--out", help="输出 CSV 路径，不给就打到终端")

    probe = sub.add_parser("probe", help="只打一次接口，打印原始响应（排查用）")
    _add_common(probe)

    cal = sub.add_parser("calibrate", help="用真实请求 URL 反推签名拼法（纯离线）")
    cal.add_argument("--url", required=True, help="一条真实接口请求的完整 URL")
    cal.add_argument("--window", type=float, default=600.0,
                     help="时间戳回溯窗口（秒），默认 600")

    ckp = sub.add_parser("checkperm", help="问服务端这个账号有没有某些功能权限")
    _add_auth(ckp)
    ckp.add_argument("--perm", action="append", default=[],
                     help="权限点，可重复；不给我就查每日订购相关的那几个")

    sub.add_parser("selftest", help="离线自检：用真实样本验证签名实现")

    args = parser.parse_args(argv)

    try:
        if args.command == "selftest":
            return selftest()

        if args.command == "login":
            return run_login(args)

        if args.command == "calibrate":
            label, ts = calibrate(args.url, window=args.window)
            print("签名拼法已对上：")
            print(f"  拼法    {label}")
            print(f"  时间戳  {ts}")
            if "sorted_all" not in label or "tail=none" not in label:
                print("\n注意：这与脚本内置的通式不同，把结果告诉我，我来更新实现。")
            return 0

        token, token_from = resolve_token(args.token, args.token_file)

        # probe 的用处就是诊断，所以允许空 token 跑（会看到 100043 token为空）。
        if not token and args.command != "probe":
            print("缺 token。用 --token、--token-file，或把 token 放到 ~/.qdama_token。",
                  file=sys.stderr)
            print("取法：浏览器 Console 里跑 localStorage.getItem('token')", file=sys.stderr)
            return 2

        if args.command == "checkperm":
            perms = args.perm or DEFAULT_PERMISSIONS
            result = check_permission(token, perms, domain=args.domain, tenant=args.tenant,
                                      shop_id=args.shop_id, sys_user_id=args.sys_user_id,
                                      api_env=args.api_env)
            print(f"token 来源 {token_from} | 环境 {args.domain}\n")
            for item in result:
                if isinstance(item, dict):
                    flag = "有" if item.get("available") else "无"
                    print(f"  [{flag}] {item.get('permission')}  {item.get('name') or ''}")
                else:
                    print(f"  {item}")
            if not result:
                print("  （服务端没返回权限数据）")
            return 0

        if not args.shop_id:
            print(f"提示：没给 --shop-id，也没有登录上下文，X-QDM-Shop-Id 暂用 --shop"
                  f"（{args.shop}）顶替。这个头必须是**账号有权访问的门店**——只有当"
                  "查询门店恰好就是你有权限的门店时才对。\n"
                  "  跑一次 login 就会自动带上它。若本次回 100006，多半是这里，"
                  "而不是真的没开通权限。", file=sys.stderr)

        days = resolve_dates(args.date, args.days, args.frm, args.to)

        if args.command == "probe":
            day = days[0]
            api_base, md5key = ENVIRONMENTS[args.domain]
            body = {"arrivaldate": day, "sapshopid": args.shop}
            url, stamp, method, data = build_request(
                api_base, DAILY_LIST_PATH, body=body, md5key=md5key, method="POST",
            )
            headers = auth_headers(token, stamp, args.tenant,
                                   args.shop_id or args.shop, args.sys_user_id,
                                   args.api_env)
            print(f"{method} {url}")
            print(f"body  {json_body(body)}\n")
            print(f"日期 {day} | 门店 {args.shop} | token 来源 {token_from}\n")
            print("请求头：")
            for name, value in headers.items():
                shown = value if name != "B2B-Authorization" else f"<{len(value)} 字符>"
                print(f"  {name}: {shown}")
            print()
            payload = call_api(url, headers, method=method, data=data)
            print(json.dumps(payload, ensure_ascii=False, indent=2)[:1200])
            print(f"\n{explain(payload)}")
            return 0 if payload.get("code") == 0 else 1

        multi = len(days) > 1
        # token 是自动探测来的就说一声，免得 token 过期了却搞不清用的哪个文件
        if token_from.startswith("/"):
            print(f"（token 来自 {token_from}）", file=sys.stderr)
        rows: list[dict] = []
        failures: list[tuple[str, str]] = []
        for index, day in enumerate(days, 1):
            label = f"[{index}/{len(days)}] {day}" if multi else day
            try:
                part = fetch_daily_summary(
                    day, args.shop, token,
                    domain=args.domain, tenant=args.tenant,
                    shop_id=args.shop_id, sys_user_id=args.sys_user_id,
                    api_env=args.api_env,
                )
            except ApiError as error:
                first_line = str(error).splitlines()[0]
                failures.append((day, str(error)))
                print(f"{label} 失败：{first_line}", file=sys.stderr)
                continue
            if multi:
                part = [{"__date": day, **row} for row in part]
                print(f"{label} → {len(part)} 行")
            rows.extend(part)
            if index < len(days) and args.sleep > 0:
                time.sleep(args.sleep)

        # 一天都没拉到，说明不是「某天没数据」而是整条链路有问题，直接报错。
        if not rows and failures:
            raise ApiError(f"全部失败。第一条（{failures[0][0]}）：{failures[0][1]}")

        _write_rows(rows, args.out)
        if failures:
            missed = "、".join(day for day, _ in failures)
            print(f"\n注意：有 {len(failures)} 天没拉到（{missed}），其余已写入。", file=sys.stderr)
        return 0
    except ApiError as error:
        print(f"失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
