#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本机管理后台抓取订单中心数据。

用法:
    python3 fetch_orders.py                     控制台表格
    python3 fetch_orders.py --format csv        输出 CSV
    python3 fetch_orders.py --format json -o orders.json

后台是本机跑的 Spring Boot 服务（默认 http://localhost:8080）。它有两个坑：

1. 登录接口有 CSRF 校验，不能直接裸 POST，必须先 GET /login 把 token 和
   JSESSIONID 拿下来，再把 token 一起提交回去。
2. 订单页是服务端渲染的 HTML 表格，没有 JSON 接口，所以得解析 DOM。

只依赖标准库。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.cookiejar import CookieJar

DEFAULT_BASE_URL = "http://localhost:8080"
# 本地测试环境的默认凭据，登录页 HTML 里就是这么写死的。
# 生产环境请走 --username/--password 或环境变量，别用这些默认值。
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin123"

DETAIL_COLUMN = "详情链接"
_CSRF_RE = re.compile(r'name="_csrf"\s+value="([^"]+)"')


class FetchError(Exception):
    """可预期的失败，消息直接展示给用户，不打堆栈。"""


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #

class AdminClient:
    """维持登录态的最小 HTTP 客户端。"""

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._cookie_jar = CookieJar()
        # 显式禁用代理：localhost 不该被沙箱或系统注入的代理接管，
        # 否则一个本地请求会被扔到外网代理上然后莫名其妙地超时。
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self._cookie_jar),
        )

    def _request(
        self, method: str, path: str, data: dict[str, str] | None = None
    ) -> tuple[str, str]:
        url = path if path.startswith(("http://", "https://")) else self.base_url + path
        headers = {"User-Agent": "fetch-orders/1.0"}
        body: bytes | None = None

        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                # 重定向会由 urllib 自动跟随，最终地址由 geturl() 给出
                return response.read().decode("utf-8", errors="replace"), response.geturl()
        except urllib.error.HTTPError as error:
            raise FetchError(f"{url} 返回 HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise FetchError(
                f"连不上 {url}（{error.reason}）。确认后台服务已经启动了。"
            ) from error

    def login(self, username: str, password: str) -> str:
        """走完整的 CSRF 登录流程，失败时抛 FetchError。"""
        page, _ = self._request("GET", "/login")

        match = _CSRF_RE.search(page)
        if match is None:
            raise FetchError("登录页里没找到 CSRF token，页面结构可能变了。")

        _, final_url = self._request(
            "POST",
            "/login",
            {
                "username": username,
                "password": password,
                "_csrf": match.group(1),
            },
        )

        # 登录失败时 Spring Security 会把人踢回 /login?error
        if "/login" in final_url:
            raise FetchError("登录被拒，账号或密码不对。")
        return final_url

    def fetch_orders_html(self) -> str:
        html, _ = self._request("GET", "/orders")
        return html


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

class _OrdersTableParser(HTMLParser):
    """只关心第一个 <table> 的 thead/tbody 结构，其余标签全部无视。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headers: list[str] = []
        self.rows: list[list[dict[str, str | None]]] = []
        self._section: str | None = None
        self._row: list[dict[str, str | None]] | None = None
        self._cell_chunks: list[str] | None = None
        self._cell_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("thead", "tbody"):
            self._section = tag
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell_chunks = []
            self._cell_href = None
        elif tag == "a" and self._cell_chunks is not None:
            href = dict(attrs).get("href")
            if href:
                self._cell_href = href

    def handle_endtag(self, tag: str) -> None:
        if tag in ("thead", "tbody"):
            self._section = None
        elif tag == "tr":
            if self._row:
                if self._section == "thead":
                    self.headers = [str(cell["text"]) for cell in self._row]
                else:
                    self.rows.append(self._row)
            self._row = None
        elif tag in ("td", "th"):
            if self._row is not None and self._cell_chunks is not None:
                self._row.append(
                    {"text": "".join(self._cell_chunks).strip(), "href": self._cell_href}
                )
            self._cell_chunks = None
            self._cell_href = None

    def handle_data(self, data: str) -> None:
        if self._cell_chunks is not None:
            self._cell_chunks.append(data)


def parse_orders(html: str, base_url: str) -> tuple[list[str], list[dict[str, str]]]:
    """把订单页 HTML 解析成 (列名, 记录列表)。"""
    parser = _OrdersTableParser()
    parser.feed(html)
    parser.close()

    if not parser.headers:
        raise FetchError("页面上没找到订单表格，可能是权限不足或者页面结构变了。")

    records: list[dict[str, str]] = []
    for row in parser.rows:
        if len(row) != len(parser.headers):
            # 列数对不上说明结构变了，宁可跳过也别把数据错位塞进去
            print(
                f"警告：跳过一行（{len(row)} 列，表头是 {len(parser.headers)} 列）",
                file=sys.stderr,
            )
            continue

        record = {
            header: str(cell["text"]) for header, cell in zip(parser.headers, row)
        }
        href = next((cell["href"] for cell in row if cell["href"]), None)
        if href:
            record[DETAIL_COLUMN] = urllib.parse.urljoin(base_url + "/", href)
        records.append(record)

    return parser.headers, records


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

def display_width(text: str) -> int:
    """终端里的显示宽度，中日韩全角字符算 2 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def output_columns(headers: list[str], records: list[dict[str, str]]) -> list[str]:
    columns = list(headers)
    if records and any(DETAIL_COLUMN in record for record in records):
        columns.append(DETAIL_COLUMN)
    return columns


def render_table(columns: list[str], records: list[dict[str, str]]) -> str:
    widths = []
    for column in columns:
        width = display_width(column)
        for record in records:
            width = max(width, display_width(record.get(column, "")))
        widths.append(width)

    lines = [
        "  ".join(pad(column, width) for column, width in zip(columns, widths)),
        "  ".join("-" * width for width in widths),
    ]
    for record in records:
        lines.append(
            "  ".join(
                pad(record.get(column, ""), width)
                for column, width in zip(columns, widths)
            )
        )
    return "\n".join(lines)


def render_csv(columns: list[str], records: list[dict[str, str]], with_bom: bool) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    text = buffer.getvalue()
    # Excel 认 BOM，不加的话中文表头会变成乱码
    return ("\ufeff" + text) if with_bom else text


def render_json(records: list[dict[str, str]]) -> str:
    return json.dumps(records, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="抓取本机管理后台的订单中心数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="后台地址，默认 %(default)s")
    parser.add_argument("--username", default=DEFAULT_USERNAME, help="登录账号")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="登录密码")
    parser.add_argument(
        "--format", choices=("table", "csv", "json"), default="table", help="输出格式"
    )
    parser.add_argument("-o", "--out", help="写入文件；不给就打到终端")
    parser.add_argument("--timeout", type=float, default=15.0, help="单次请求超时秒数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    client = AdminClient(args.base_url, timeout=args.timeout)

    try:
        client.login(args.username, args.password)
        html = client.fetch_orders_html()
        headers, records = parse_orders(html, args.base_url)
    except FetchError as error:
        print(f"抓取失败：{error}", file=sys.stderr)
        return 1

    columns = output_columns(headers, records)

    if args.format == "table":
        output = render_table(columns, records)
    elif args.format == "csv":
        output = render_csv(columns, records, with_bom=bool(args.out))
    else:
        output = render_json(records)

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as handle:
            handle.write(output + "\n")
        print(f"已写入 {args.out}，共 {len(records)} 条订单。")
    else:
        print(output)
        print(f"\n共 {len(records)} 条订单。", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
