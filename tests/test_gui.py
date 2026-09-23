"""GUI 逻辑层的离线测试：``probe_site`` / ``fetch_menus`` / ``explain_error``。

这里的靶子是一个**真的 HTTP 服务**（标准库 ``http.server``），不是把
``WebSession`` mock 掉。这样 cookie、302 跟随、CSRF 隐藏字段、登出链接过滤
这些真实行为会被一起验到，而且**不依赖任何外部服务**——没有后台也能跑。

界面本身（tkinter 控件）不在这里测：那部分靠 ``--smoke`` 和 ``--auto`` 两个
自检入口覆盖，它们会把数据真填进控件再读回来核对。
"""

from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

try:
    import tkinter  # noqa: F401
except ImportError:  # pragma: no cover - 极少数精简环境
    tkinter = None

if tkinter is None:  # pragma: no cover
    raise unittest.SkipTest("没有 tkinter，跳过 GUI 层测试")

from websnap.auth import LoginError
from websnap.config import ConfigError, Profile
from websnap.gui import explain_error, fetch_menus, probe_site
from websnap.session import RequestError

LOGIN_PAGE = """<!doctype html><html><head><title>登录</title></head><body>
<form method="post" action="/login">
  <input name="username" value="">
  <input type="password" name="password">
  <input type="hidden" name="_csrf" value="tok-123">
  <button type="submit">登录</button>
</form></body></html>"""

NAV = """<aside class="sidebar"><nav class="nav">
    <a href="/dashboard">经营看板</a>
    <a href="/orders">订单中心</a>
    <a href="/broken">坏掉的页</a>
    <a href="/logout">退出登录</a>
  </nav></aside>"""


def page(title: str, headers: list[str], rows: list[tuple[str, ...]], nav: bool = True) -> str:
    head = "".join(f"<th>{item}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"""<!doctype html><html><head><title>{title}</title></head><body>
<div class="layout">
  {NAV if nav else ""}
  <section class="main"><div class="card">
    <table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>
  </div></section>
</div></body></html>"""


HOME_PAGE = page("首页", ["指标", "数值"], [("营业额", "1234")])
ORDERS_PAGE = page("订单中心", ["订单号", "金额"], [("QD001", "32.36"), ("QD002", "18.00")])
DASHBOARD_PAGE = page("经营看板", ["指标", "数值"], [("订单数", "2"), ("客单价", "25.18")])


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # 登录状态放 server 上，而不是类变量——测试之间才不会互相污染
        self.authed = False


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 别把访问日志灌进测试输出
        pass

    def _send(self, status: int, body: str, headers: tuple[tuple[str, str], ...] = ()) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/login":
            return self._send(200, LOGIN_PAGE)
        if path == "/":
            return self._send(200, HOME_PAGE if self.server.authed else LOGIN_PAGE)
        if path == "/dashboard":
            return self._send(200, DASHBOARD_PAGE)
        if path == "/orders":
            return self._send(200, ORDERS_PAGE)
        if path == "/broken":
            return self._send(500, "<html><body>boom</body></html>")
        return self._send(404, "<html><body>404</body></html>")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        fields = dict(parse_qsl(self.rfile.read(length).decode("utf-8")))
        if fields.get("username") == "admin" and fields.get("password") == "secret":
            self.server.authed = True
            return self._send(302, "", (("Location", "/"), ("Set-Cookie", "SID=ok; Path=/")))
        return self._send(302, "", (("Location", "/login?error"),))


class GuiLogicTests(unittest.TestCase):
    """跑在真实 HTTP 上，但完全离线。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _Server(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.server.authed = False
        self.base = f"http://127.0.0.1:{self.port}"
        self.profile = Profile(base_url=self.base, timeout=5.0)

    def _probe(self):
        return probe_site(self.base, "admin", "secret", profile=self.profile)

    # -------------------------------------------------------------- 探测

    def test_probe_logs_in_and_finds_menus(self):
        probe = self._probe()
        names = [menu.name for menu in probe.menus]
        self.assertIn("订单中心", names)
        self.assertIn("经营看板", names)
        # 登出链接不该被当成菜单
        self.assertNotIn("退出登录", names)
        self.assertIn("POST", probe.detail)

    def test_probe_rejects_wrong_password(self):
        with self.assertRaises(LoginError):
            probe_site(self.base, "admin", "wrong", profile=self.profile)

    def test_probe_requires_credentials_when_not_skipping(self):
        with self.assertRaises(ConfigError):
            probe_site(self.base, "admin", "", profile=self.profile)

    def test_probe_can_skip_login_for_open_systems(self):
        self.server.authed = True  # 模拟"首页不需要登录就能看"的系统
        probe = probe_site(self.base, profile=self.profile, skip_login=True)
        self.assertIsNone(probe.form)
        self.assertEqual(probe.detail, "跳过登录")
        self.assertGreaterEqual(len(probe.menus), 2)

    def test_probe_reports_missing_nav_instead_of_guessing(self):
        with self.assertRaises(ConfigError) as caught:
            probe_site(f"{self.base}/nope", profile=self.profile, skip_login=True)
        self.assertIn("没能", str(caught.exception))

    # -------------------------------------------------------------- 抓取

    def test_fetch_menus_extracts_tables(self):
        probe = self._probe()
        menus = [menu for menu in probe.menus if menu.name in ("经营看板", "订单中心")]
        results = fetch_menus(probe, menus)

        self.assertEqual(len(results), 2)
        orders = next(dataset for menu, dataset, _ in results if menu.name == "订单中心")
        self.assertEqual(orders.count, 2)
        self.assertEqual(orders.columns[:2], ["订单号", "金额"])
        self.assertEqual(orders.rows[0]["金额"], "32.36")

    def test_fetch_menus_skips_broken_page_without_aborting(self):
        probe = self._probe()
        menus = [menu for menu in probe.menus if menu.name in ("订单中心", "坏掉的页")]
        events: list[str] = []
        results = fetch_menus(
            probe, menus, on_event=lambda kind, message=None: events.append(str(message))
        )

        names = [menu.name for menu, _, _ in results]
        self.assertIn("订单中心", names)
        self.assertNotIn("坏掉的页", names, "500 的页面应当被跳过，而不是让整批抓取挂掉")
        self.assertTrue(
            any("500" in event for event in events), f"该报告被跳过的原因，实际事件：{events}"
        )

    def test_fetch_menus_does_not_leak_nav_into_data(self):
        """侧边栏链接必须被排除，否则菜单名会混进数据行。"""
        probe = self._probe()
        menus = [menu for menu in probe.menus if menu.name == "经营看板"]
        results = fetch_menus(probe, menus)

        dataset = results[0][1]
        joined = " ".join(str(value) for row in dataset.rows for value in row.values())
        self.assertNotIn("退出登录", joined)
        self.assertNotIn("订单中心", joined)

    # -------------------------------------------------------------- 错误翻译

    def test_explain_error_maps_exception_types(self):
        self.assertIn("登录失败", explain_error(LoginError("账号或密码不对")))
        self.assertIn("请求失败", explain_error(RequestError("连不上")))
        self.assertEqual(explain_error(ConfigError("缺账号")), "缺账号")
        self.assertIn("ValueError", explain_error(ValueError("x")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
