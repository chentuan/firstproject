"""websnap 的单元测试。全部离线，不依赖任何服务。

跑法：
    python3 -m unittest discover -s tests -t .
    python3 -m websnap --selftest
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import socket
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path

from websnap.auth import LoginError, locate_login_form, parse_login_form
from websnap.cli import _write_output
from websnap.config import ConfigError, Profile, load_profile, save_profile
from websnap.dom import SelectorError, parse_html, select
from websnap.extract import (
    CARDS,
    KV,
    LIST,
    TABLE,
    TEXT,
    ExtractError,
    detect_client_rendered,
    extract,
    rendering_hint,
)
from websnap.menu import discover_menus, find_page_nav, match_menu
from websnap.output import build_payload, display_width, render_csv, render_json, render_table
from websnap.session import (
    RequestError,
    Response,
    WebSession,
    build_ssl_context,
    system_ca_bundle,
    translate_network_error,
)


# --------------------------------------------------------------------------- #
# dom
# --------------------------------------------------------------------------- #

class TreeBuilderTests(unittest.TestCase):
    def test_implicit_close_li(self):
        tree = parse_html("<ul><li>甲<li>乙<li>丙</ul>")
        items = select(tree, "ul > li")
        self.assertEqual([item.clean_text() for item in items], ["甲", "乙", "丙"])
        # 三个 li 必须同级，而不是一层套一层
        self.assertEqual(len(select(tree, "li li")), 0)

    def test_implicit_close_td(self):
        tree = parse_html("<table><tr><td>a<td>b<tr><td>c<td>d</table>")
        rows = select(tree, "table > tr")
        self.assertEqual(len(rows), 2)
        self.assertEqual([cell.clean_text() for cell in select(tree, "td")], ["a", "b", "c", "d"])

    def test_void_tags_do_not_nest(self):
        tree = parse_html('<div><br><span class="after">x</span></div>')
        span = tree.find("span.after")
        self.assertIsNotNone(span)
        # br 不压栈，span 应该是 div 的直接子元素
        self.assertEqual(span.parent.tag, "div")

    def test_script_content_is_dropped(self):
        tree = parse_html("<div>保留<script>if (a < b) { alert('x') }</script></div>")
        self.assertEqual(tree.find("div").clean_text(), "保留")

    def test_text_collapses_whitespace_and_nbsp(self):
        tree = parse_html("<td>  钱大妈\n\t· 南油店&nbsp;店 </td>")
        self.assertEqual(tree.find("td").clean_text(), "钱大妈 · 南油店 店")

    def test_br_becomes_newline(self):
        tree = parse_html("<td>上行<br>下行</td>")
        self.assertIn("\n", tree.find("td").raw_text())

    def test_attribute_names_are_lowercased(self):
        tree = parse_html('<a HREF="/orders" Class="nav">x</a>')
        anchor = tree.find("a")
        self.assertEqual(anchor.get("href"), "/orders")
        self.assertIn("nav", anchor.classes)

    def test_stray_end_tag_is_ignored(self):
        tree = parse_html("<div>a</div></span><p>b</p>")
        self.assertEqual(tree.find("div").clean_text(), "a")
        self.assertEqual(tree.find("p").clean_text(), "b")

    def test_nested_table_is_not_broken(self):
        tree = parse_html(
            "<table id=outer><tr><td>外<table id=inner><tr><td>内</td></tr></table></td></tr></table>"
        )
        self.assertEqual(tree.find("#inner").find("td").clean_text(), "内")
        self.assertEqual(tree.find("#outer").find("td").clean_text(), "外内")


class SelectorTests(unittest.TestCase):
    HTML = """
    <body>
      <aside class="sidebar main"><nav class="nav">
        <a href="/orders" class="item active">订单中心</a>
        <a href="/stores" class="item">门店管理</a>
      </nav></aside>
      <main id="content"><table class="data"><tr><td>1</td></tr></table></main>
    </body>
    """

    def setUp(self):
        self.tree = parse_html(self.HTML)

    def test_tag(self):
        self.assertEqual(len(select(self.tree, "a")), 2)

    def test_class_and_multi_class(self):
        self.assertEqual(len(select(self.tree, ".item")), 2)
        self.assertEqual(len(select(self.tree, ".item.active")), 1)

    def test_id(self):
        self.assertEqual(len(select(self.tree, "#content")), 1)

    def test_attribute_and_value(self):
        self.assertEqual(len(select(self.tree, "[href]")), 2)
        self.assertEqual(len(select(self.tree, '[href="/stores"]')), 1)

    def test_descendant_combinator(self):
        self.assertEqual(len(select(self.tree, "aside a")), 2)

    def test_child_combinator(self):
        self.assertEqual(len(select(self.tree, "nav > a")), 2)
        # table 不是 body 的直接子元素
        self.assertEqual(len(select(self.tree, "body > table")), 0)

    def test_star_and_compound(self):
        self.assertEqual(len(select(self.tree, "nav.nav")), 1)
        self.assertEqual(len(select(self.tree, "*[id=content]")), 1)

    def test_unclosed_bracket_raises(self):
        with self.assertRaises(SelectorError):
            select(self.tree, "[href")

    def test_unknown_snippet_raises(self):
        with self.assertRaises(SelectorError):
            select(self.tree, ">>")


# --------------------------------------------------------------------------- #
# extract
# --------------------------------------------------------------------------- #

ORDER_HTML = """
<div>
  <aside class="sidebar"><nav>
    <a href="/dashboard">经营看板</a><a href="/stores">门店管理</a>
    <a href="/products">商品SKU</a><a href="/inventory">门店库存</a>
    <a href="/orders">订单中心</a>
  </nav></aside>
  <main>
    <table>
      <thead><tr><th>订单号</th><th>门店</th><th>金额</th></tr></thead>
      <tbody>
        <tr><td><a href="/orders/1">QD001</a></td><td>南油店</td><td>32.36</td></tr>
        <tr><td><a href="/orders/2">QD002</a></td><td>天河店</td><td>18.00</td></tr>
      </tbody>
    </table>
  </main>
</div>
"""


class ExtractTests(unittest.TestCase):
    def test_table_columns_come_from_thead(self):
        tree = parse_html(ORDER_HTML)
        dataset = extract(tree, "http://x")
        self.assertEqual(dataset.kind, TABLE)
        self.assertEqual(dataset.columns, ["订单号", "门店", "金额"])
        self.assertEqual(dataset.count, 2)

    def test_cell_links_go_to_links_field(self):
        tree = parse_html(ORDER_HTML)
        dataset = extract(tree, "http://x")
        self.assertEqual(dataset.rows[0]["_links"]["订单号"], "http://x/orders/1")

    def test_nav_links_are_not_treated_as_data(self):
        tree = parse_html(ORDER_HTML)
        dataset = extract(tree, "http://x")
        self.assertNotIn("经营看板", json.dumps(dataset.rows, ensure_ascii=False))

    def test_excluded_nav_is_skipped_for_list_pages(self):
        html = """
        <aside class="sidebar"><nav>
          <a href="/a">菜单甲</a><a href="/b">菜单乙</a><a href="/c">菜单丙</a>
        </nav></aside>
        <main><ul><li>数据一</li><li>数据二</li><li>数据三</li></ul></main>
        """
        tree = parse_html(html)
        nav = find_page_nav(tree)
        self.assertIsNotNone(nav)

        dataset = extract(tree, "http://x", exclude_nodes=(nav,))
        self.assertEqual(dataset.kind, LIST)
        self.assertEqual([row["内容"] for row in dataset.rows], ["数据一", "数据二", "数据三"])

    def test_rag_cols_are_skipped_not_misaligned(self):
        html = """
        <table><thead><tr><th>a</th><th>b</th></tr></thead>
        <tbody><tr><td>1</td><td>2</td></tr><tr><td>3</td></tr></tbody></table>
        """
        dataset = extract(parse_html(html), "http://x")
        self.assertEqual(dataset.count, 1)
        self.assertIn("跳过", dataset.note)

    def test_duplicate_headers_are_deduped(self):
        html = """
        <table><thead><tr><th>状态</th><th>状态</th></tr></thead>
        <tbody><tr><td>开</td><td>关</td></tr></tbody></table>
        """
        dataset = extract(parse_html(html), "http://x")
        self.assertEqual(dataset.columns, ["状态", "状态_1"])
        self.assertEqual(dataset.rows[0]["状态_1"], "关")

    def test_empty_header_with_links_becomes_action_column(self):
        html = """
        <table><thead><tr><th>店名</th><th></th></tr></thead>
        <tbody><tr><td>南油店</td><td><a href="/s/1/edit">编辑</a></td></tr></tbody></table>
        """
        dataset = extract(parse_html(html), "http://x")
        self.assertEqual(dataset.columns, ["店名", "操作"])

    def test_colspan_header_is_expanded(self):
        html = """
        <table><thead><tr><th>名称</th><th colspan="2">区间</th></tr></thead>
        <tbody><tr><td>x</td><td>1</td><td>2</td></tr></tbody></table>
        """
        dataset = extract(parse_html(html), "http://x")
        self.assertEqual(dataset.columns, ["名称", "区间", "区间_1"])

    def test_two_column_headerless_table_is_kv(self):
        html = "<table><tr><td>门店编号</td><td>QDM-SZ-001</td></tr><tr><td>店长</td><td>李店长</td></tr></table>"
        dataset = extract(parse_html(html), "http://x")
        self.assertEqual(dataset.kind, KV)
        self.assertEqual(dataset.rows[1], {"字段": "店长", "值": "李店长"})

    def test_dl_is_kv(self):
        html = "<dl><dt>订单号</dt><dd>QD001</dd><dt>金额</dt><dd>32.36</dd></dl>"
        dataset = extract(parse_html(html), "http://x")
        self.assertEqual(dataset.kind, KV)
        self.assertEqual(dataset.count, 2)

    def test_repeated_cards(self):
        html = """
        <div class="grid">
          <div class="card"><span>甲</span><span>1</span></div>
          <div class="card"><span>乙</span><span>2</span></div>
          <div class="card"><span>丙</span><span>3</span></div>
        </div>
        """
        dataset = extract(parse_html(html), "http://x")
        self.assertEqual(dataset.kind, CARDS)
        self.assertEqual(dataset.count, 3)
        self.assertEqual(dataset.rows[1], {"字段1": "乙", "字段2": "2"})

    def test_text_fallback(self):
        html = "<main><h1>经营看板</h1><p>今日营业额 1234 元</p><p>环比上升</p></main>"
        dataset = extract(parse_html(html), "http://x")
        self.assertEqual(dataset.kind, TEXT)
        self.assertIn("今日营业额 1234 元", [row["内容"] for row in dataset.rows])

    def test_selector_scope_narrows_extraction(self):
        html = """
        <table id="a"><thead><tr><th>x</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>
        <table id="b"><thead><tr><th>y</th><th>z</th></tr></thead>
        <tbody><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></tbody></table>
        """
        dataset = extract(parse_html(html), "http://x", selector="#b")
        self.assertEqual(dataset.columns, ["y", "z"])
        self.assertEqual(dataset.count, 2)

    def test_bad_selector_raises(self):
        with self.assertRaises(ExtractError):
            extract(parse_html("<div/>"), "http://x", selector="#missing")

    def test_forced_mode_without_match_raises(self):
        with self.assertRaises(ExtractError):
            extract(parse_html("<div>纯文本</div>"), "http://x", mode="table")


# --------------------------------------------------------------------------- #
# menu
# --------------------------------------------------------------------------- #

SIDEBAR_HTML = """
<body>
  <aside class="sidebar">
    <nav class="nav">
      <a href="/dashboard" class="active">经营看板</a>
      <a href="/stores">门店管理</a>
      <a href="/products">商品SKU</a>
      <a href="/orders">订单中心</a>
      <a href="/logout">退出登录</a>
      <a href="#">回到顶部</a>
      <a href="javascript:void(0)">展开</a>
      <a href="/orders">订单中心（重复）</a>
    </nav>
  </aside>
  <main>正文<a href="/orders/1">QD001</a></main>
</body>
"""


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.menus, self.match = discover_menus(parse_html(SIDEBAR_HTML), "http://localhost:8080")

    def test_finds_menus_from_semantic_container(self):
        self.assertIsNotNone(self.match.selector)
        self.assertEqual(
            [menu.name for menu in self.menus],
            ["经营看板", "门店管理", "商品SKU", "订单中心"],
        )

    def test_logout_and_dead_links_are_filtered(self):
        names = [menu.name for menu in self.menus]
        self.assertNotIn("退出登录", names)
        self.assertNotIn("回到顶部", names)
        self.assertNotIn("展开", names)

    def test_duplicate_href_is_deduped(self):
        paths = [menu.path for menu in self.menus]
        self.assertEqual(len(paths), len(set(paths)))

    def test_content_links_are_outside_the_container(self):
        self.assertNotIn("QD001", [menu.name for menu in self.menus])

    def test_paths_are_absolute(self):
        self.assertTrue(all(menu.path.startswith("http://localhost:8080/") for menu in self.menus))

    def test_density_fallback_when_no_semantic_marker(self):
        html = """
        <div class="wrapper">
          <div class="left"><a href="/a">甲</a><a href="/b">乙</a><a href="/c">丙</a></div>
          <div class="right">正文正文正文正文正文正文正文正文正文正文正文正文正文正文正文</div>
        </div>
        """
        menus, match = discover_menus(parse_html(html), "http://x")
        self.assertIsNone(match.selector)
        self.assertIn("密度", match.reason)
        self.assertEqual([menu.name for menu in menus], ["甲", "乙", "丙"])

    def test_no_nav_returns_empty(self):
        menus, match = discover_menus(parse_html("<div>什么都没有</div>"), "http://x")
        self.assertEqual(menus, [])
        self.assertFalse(match.found)

    def test_match_menu_by_index_name_and_path(self):
        self.assertEqual(match_menu(self.menus, "2").name, "门店管理")
        self.assertEqual(match_menu(self.menus, "订单中心").name, "订单中心")
        self.assertEqual(match_menu(self.menus, "/stores").name, "门店管理")
        self.assertEqual(match_menu(self.menus, "库存"), None)
        self.assertEqual(match_menu(self.menus, "商品").name, "商品SKU")


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #

class LoginFormTests(unittest.TestCase):
    def test_parses_spring_security_style_form(self):
        html = """
        <form action="/login" method="post">
          <input type="hidden" name="_csrf" value="tok-123">
          <input name="username" value="admin">
          <input type="password" name="password" value="admin123">
          <button type="submit">登录</button>
        </form>
        """
        form = parse_login_form(html, "http://localhost:8080/login")
        self.assertIsNotNone(form)
        self.assertEqual(form.action, "http://localhost:8080/login")
        self.assertEqual(form.method, "POST")
        self.assertEqual(form.username_field, "username")
        self.assertEqual(form.password_field, "password")
        self.assertEqual(form.token_fields, ("_csrf",))

    def test_payload_carries_hidden_fields_back(self):
        html = """
        <form action="/login" method="post">
          <input type="hidden" name="csrfmiddlewaretoken" value="abc">
          <input type="hidden" name="next" value="/orders">
          <input name="account"><input type="password" name="pwd">
        </form>
        """
        form = parse_login_form(html, "http://x/login")
        payload = form.payload("cian", "secret")
        self.assertEqual(payload["csrfmiddlewaretoken"], "abc")
        self.assertEqual(payload["next"], "/orders")
        self.assertEqual(payload["account"], "cian")
        self.assertEqual(payload["pwd"], "secret")

    def test_page_without_password_input_is_not_a_login_form(self):
        html = '<form><input name="q"><button>搜索</button></form>'
        self.assertIsNone(parse_login_form(html, "http://x"))

    def test_relative_action_is_resolved(self):
        html = '<form action="doLogin" method="post"><input name="u"><input type="password" name="p"></form>'
        form = parse_login_form(html, "http://x/admin/login")
        self.assertEqual(form.action, "http://x/admin/doLogin")


class LoginLocateFailureTests(unittest.TestCase):
    """找不到登录表单时，报错必须交代清楚"试了什么、为什么不行"。

    由来：实测一个线上 Vue 后台，程序把 10 个候选地址全试了一遍，最后只丢一句
    "请用 --login-path 指定登录页地址"。可那个站的表单压根不在 HTML 里——配什么
    路径都没用。这种把锅甩给用户的报错，比不给报错更浪费时间：它会让人真的去
    折腾配置，而正确的动作是换技术路线。
    """

    class _StubSession:
        """按路径返回预置页面；没预置的走 ``default``（默认 404）。

        ``default`` 是给单页应用准备的：那类站点对**任何**路径都返回同一个入口
        文件（路由在前端），所以要模拟它就得让所有路径吐同一份 HTML。
        """

        def __init__(self, pages=None, default=None):
            self.pages = pages or {}
            self.default = default or (404, "<html><body>not found</body></html>")
            self.history = []

        def get(self, path, headers=None):
            self.history.append(("GET", path))
            status, text = self.pages.get(path, self.default)
            return Response(url="http://x.example" + path, status=status, text=text, headers={})

    def test_same_spa_shell_on_every_path_is_called_out(self):
        session = self._StubSession(default=(200, SPA_HTML))
        with self.assertRaises(LoginError) as ctx:
            locate_login_form(session)
        message = str(ctx.exception)
        self.assertIn("前端空壳", message)
        self.assertIn("单页应用", message)
        # 关键：不能再建议用户去改配置——那不是问题所在
        self.assertNotIn("请用 --login-path", message)
        # 得给出真正的出路
        self.assertIn("headless", message)

    def test_failure_lists_every_attempted_address(self):
        session = self._StubSession({"/": (200, SERVER_RENDERED_HTML)})
        with self.assertRaises(LoginError) as ctx:
            locate_login_form(session)
        message = str(ctx.exception)
        self.assertIn("GET /", message)
        self.assertIn("GET /login", message)
        self.assertIn("404", message)
        self.assertIn("--login-path", message)

    def test_unreachable_everywhere_reports_network_error(self):
        class _Dead:
            def get(self, path, headers=None):
                raise RequestError("x.example 拒绝连接")

        with self.assertRaises(LoginError) as ctx:
            locate_login_form(_Dead())
        self.assertIn("拒绝连接", str(ctx.exception))

    def test_login_path_is_tried_first(self):
        """显式给的 login_path 必须排在候选列表最前面，别被内置路径抢先。"""
        session = self._StubSession({"/special/signin": (200, SPA_HTML)})
        with self.assertRaises(LoginError):
            locate_login_form(session, login_path="/special/signin")
        self.assertEqual(session.history[0], ("GET", "/special/signin"))


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

class OutputTests(unittest.TestCase):
    def _dataset(self):
        return extract(parse_html(ORDER_HTML), "http://x")

    def test_display_width_counts_cjk_as_two(self):
        self.assertEqual(display_width("abc"), 3)
        self.assertEqual(display_width("订单号"), 6)
        self.assertEqual(display_width("a订"), 3)

    def test_table_is_aligned(self):
        lines = render_table(self._dataset()).splitlines()
        widths = {display_width(line) for line in lines}
        self.assertEqual(len(widths), 1, f"列宽不齐：{sorted(widths)}")

    def test_csv_has_bom_when_writing_file(self):
        self.assertTrue(render_csv(self._dataset(), with_bom=True).startswith("\ufeff"))
        self.assertFalse(render_csv(self._dataset(), with_bom=False).startswith("\ufeff"))

    def test_payload_shape(self):
        payload = build_payload(
            self._dataset(), site="测试站", menu_name="订单中心", menu_path="http://x/orders"
        )
        self.assertEqual(payload["menu"]["name"], "订单中心")
        self.assertEqual(payload["kind"], TABLE)
        self.assertEqual(payload["count"], 2)
        self.assertNotIn("_links", payload["columns"])
        self.assertEqual(json.loads(render_json(payload))["rows"][0]["门店"], "南油店")


class WriteOutputTests(unittest.TestCase):
    """终端输出必须覆盖到每一个菜单。

    回归用例：``_write_output`` 曾经在 table 格式下只渲染 ``results[0]``，
    抓了 9 个菜单却只显示第一个，而且不报任何错 —— 属于最阴的一类 bug。
    """

    def _results(self):
        tree = parse_html(ORDER_HTML)
        menus, _ = discover_menus(tree, "http://x")
        dataset = extract(tree, "http://x")
        # discover_menus 返回的 path 已是绝对地址
        return menus, [(menu, dataset, menu.path) for menu in menus]

    def test_table_stdout_covers_every_menu(self):
        menus, results = self._results()
        self.assertGreater(len(results), 1, "样本得有多于一个菜单，否则测不出这个 bug")

        args = argparse.Namespace(out=None, format=None)
        profile = Profile(name="测试站", base_url="http://x")
        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            _write_output(args, results, profile, "http://x")

        output = buffer.getvalue()
        for menu in menus:
            self.assertIn(menu.name, output, f"菜单「{menu.name}」在终端输出里缺席了")
        # 每个菜单一段表头，段数应当等于菜单数。
        self.assertEqual(output.count("· 表格"), len(results))

    def test_csv_multi_menu_to_single_file_is_rejected(self):
        """多菜单往同一个 CSV 文件里写会静默覆盖，必须拦下来。"""
        _, results = self._results()
        args = argparse.Namespace(out="/tmp/websnap-测试.csv", format=None)
        profile = Profile(name="测试站", base_url="http://x")
        with self.assertRaises(ConfigError):
            _write_output(args, results, profile, "http://x")


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

class ConfigTests(unittest.TestCase):
    def test_loads_valid_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site.toml"
            path.write_text(
                """
[site]
name = "钱大妈后台"
base_url = "http://localhost:8080"
timeout = 5

[auth]
mode = "auto"
login_path = "/login"
username = "admin"

[menus]
nav_selectors = ["aside.sidebar", "nav.nav"]

[extract]
mode = "table"
""",
                encoding="utf-8",
            )
            profile, warnings = load_profile(path)
            self.assertEqual(warnings, [])
            self.assertEqual(profile.name, "钱大妈后台")
            self.assertEqual(profile.timeout, 5.0)
            self.assertEqual(profile.nav_selectors, ("aside.sidebar", "nav.nav"))
            self.assertEqual(profile.extract_mode, "table")
            self.assertEqual(profile.username_field, None)

    def test_unknown_key_warns_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site.toml"
            path.write_text('[site]\nbase_url = "http://x"\ntimout = 5\n', encoding="utf-8")
            _, warnings = load_profile(path)
            self.assertEqual(len(warnings), 1)
            self.assertIn("timout", warnings[0])

    def test_bad_mode_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site.toml"
            path.write_text('[extract]\nmode = "magic"\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_profile(path)

    def test_broken_toml_raises_with_location(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site.toml"
            path.write_text("[site\nbase_url = 1\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_profile(path)

    def test_save_roundtrip_never_writes_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out" / "site.toml"
            profile = Profile(
                name="本地后台",
                base_url="http://localhost:8080",
                username="admin",
                login_path="/login",
                username_field="username",
                password_field="password",
                nav_selectors=("aside",),
            )
            save_profile(path, profile)
            text = path.read_text(encoding="utf-8")

            # 密码绝不能落盘：配置进版本库是常态，凭据进版本库是事故。
            assignments = [
                line.split("=", 1)[0].strip()
                for line in text.splitlines()
                if "=" in line and not line.lstrip().startswith("#")
            ]
            self.assertNotIn("password", assignments)
            self.assertIn("password_field", assignments)
            self.assertIn('base_url = "http://localhost:8080"', text)

            reloaded, warnings = load_profile(path)
            self.assertEqual(warnings, [])
            self.assertEqual(reloaded.base_url, "http://localhost:8080")
            self.assertEqual(reloaded.username, "admin")
            self.assertEqual(reloaded.nav_selectors, ("aside",))


# --------------------------------------------------------------------------- #
# 网络层：证书兜底与错误分类
# --------------------------------------------------------------------------- #


class NetworkErrorTests(unittest.TestCase):
    """网络故障必须分类说清楚。

    这组测试的由来：曾经所有 ``URLError`` 都被翻译成"检查地址是否正确、服务是否
    已启动"。实测一台 macOS 官方安装包的 Python 连合法 HTTPS 站点时，报的正是
    这句话——而连接是通的、服务也活着，真因是本机 CA 集合为空。
    """

    @staticmethod
    def _wrapped(reason: BaseException) -> urllib.error.URLError:
        return urllib.error.URLError(reason)

    def test_certificate_failure_is_not_blamed_on_the_service(self):
        error = self._wrapped(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        message = translate_network_error("https://x.example/", error)
        self.assertIn("证书", message)
        self.assertIn("--insecure", message)
        self.assertNotIn("服务没起来", message)

    def test_connection_refused_points_at_the_port(self):
        error = self._wrapped(ConnectionRefusedError(61, "Connection refused"))
        self.assertIn("拒绝连接", translate_network_error("https://x.example/", error))

    def test_dns_failure_points_at_the_address(self):
        error = self._wrapped(socket.gaierror(8, "nodename nor servname provided"))
        self.assertIn("域名解析不了", translate_network_error("https://x.example/", error))

    def test_timeout_is_reported_as_timeout(self):
        error = self._wrapped(socket.timeout("timed out"))
        self.assertIn("超时", translate_network_error("https://x.example/", error))

    def test_bare_exception_without_reason_attribute(self):
        """裸异常（没有 .reason）也要能翻译，不能 AttributeError。"""
        message = translate_network_error("https://x.example/", ConnectionRefusedError(61, "refused"))
        self.assertIn("拒绝连接", message)


class SslContextTests(unittest.TestCase):
    """根证书兜底。

    macOS 官方安装包的 Python 只认 ``SSL_CERT_FILE``，默认一张根证书都没有，
    导致任何 HTTPS 请求都报 ``CERTIFICATE_VERIFY_FAILED``。工具自己去借系统证书库，
    而不是让使用者去修 Python 环境。
    """

    def test_build_context_has_trusted_cas(self):
        if not system_ca_bundle():
            self.skipTest("本机没有系统证书库，跳过")
        self.assertGreater(len(build_ssl_context(True).get_ca_certs()), 0)

    def test_insecure_context_disables_both_checks(self):
        context = build_ssl_context(False)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)

    def test_missing_bundle_override_is_ignored(self):
        with _temporary_env("WEBSNAP_CA_BUNDLE", "/nonexistent/ca-bundle.pem"):
            self.assertIsNone(system_ca_bundle())

    def test_existing_bundle_override_wins(self):
        bundle = system_ca_bundle()
        if not bundle:
            self.skipTest("本机没有系统证书库，跳过")
        with _temporary_env("WEBSNAP_CA_BUNDLE", bundle):
            self.assertEqual(system_ca_bundle(), bundle)

    def test_session_attaches_https_handler_in_both_modes(self):
        """两种模式都要显式挂 HTTPSHandler，否则 verify=False 会被 urllib 忽略。"""
        for verify in (True, False):
            with self.subTest(verify=verify):
                session = WebSession("https://x.example", verify=verify)
                names = [type(handler).__name__ for handler in session._opener.handlers]
                self.assertIn("HTTPSHandler", names)


# --------------------------------------------------------------------------- #
# 前端渲染识别
# --------------------------------------------------------------------------- #

# 一个真实的 Vue 后台首页长这样：3300 字节、body 里一个空的挂载点，
# 数据全靠后面那两个打包 JS 去拉。
SPA_HTML = """<!DOCTYPE html><html lang=en><head><meta charset=utf-8><title>某智慧中台</title>
<script>if(window.location.href.indexOf('#/')===-1){window.location.href=window.location.origin+'/#'+window.location.pathname;}</script>
<link href=/js/app.fb38e308.css rel=preload as=style></head>
<body id=mbodyid><noscript><strong></strong></noscript><div id=app></div>
<script src=/js/chunk-vendors.04f9cd1d.js></script><script src=/js/app.31165e54.js></script>
</body></html>"""

SERVER_RENDERED_HTML = """<!DOCTYPE html><html><head><title>订单中心</title></head><body>
<aside class="sidebar"><a href="/orders">订单中心</a><a href="/stores">门店管理</a></aside>
<section class="main"><h1>订单中心</h1>
<table><thead><tr><th>订单号</th><th>门店</th></tr></thead>
<tbody><tr><td>QD001</td><td>南油店</td></tr><tr><td>QD002</td><td>天河店</td></tr></tbody></table>
</section></body></html>"""


class ClientRenderedTests(unittest.TestCase):
    """SPA 和"这页本来就没数据"必须分开说。

    这组测试的由来：实测一个线上 Vue 后台，首页只有 3322 字节，body 里除一个空的
    ``<div id=app>`` 什么都没有。此时不管 selector 怎么调都抓不到——问题不在配置上，
    而在技术路线，得让工具说清楚，别让人对着空结果反复改参数。
    """

    def test_detects_spa_shell(self):
        evidence = detect_client_rendered(parse_html(SPA_HTML))
        self.assertTrue(evidence)
        joined = " ".join(evidence)
        self.assertIn('id="app"', joined)
        self.assertIn("打包产物", joined)

    def test_server_rendered_page_is_not_flagged(self):
        self.assertEqual(detect_client_rendered(parse_html(SERVER_RENDERED_HTML)), [])

    def test_empty_result_carries_the_real_reason(self):
        dataset = extract(parse_html(SPA_HTML), "https://x.example/")
        self.assertEqual(dataset.count, 0)
        self.assertIn("前端 JS 渲染", dataset.note)
        self.assertIn("headless", dataset.note)

    def test_normal_page_gets_no_rendering_hint(self):
        self.assertEqual(rendering_hint(parse_html(SERVER_RENDERED_HTML)), "")

    def test_rendering_hint_names_the_bundle(self):
        """提示里要点出具体的产物文件名，不然使用者没法自己判断是不是打包出来的。"""
        self.assertIn("chunk-vendors.04f9cd1d.js", rendering_hint(parse_html(SPA_HTML)))


@contextlib.contextmanager
def _temporary_env(name: str, value: str):
    """临时改一个环境变量，退出时还原。"""
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


if __name__ == "__main__":
    unittest.main(verbosity=2)
