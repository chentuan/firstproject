"""命令行入口：交互问答模式与参数模式。

两种模式走的是同一套逻辑，区别只在"参数从哪来"——所以 ``--menu 订单中心``
和交互式输入 "5" 最终落到同一个函数上。参数模式是为了能挂定时任务和进脚本，
交互模式是为了第一次用的时候不用读文档。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

from . import __version__
from .auth import LoginError, LoginForm, login
from .config import ConfigError, Profile, find_profile, load_profile, save_profile
from .dom import SelectorError, parse_html
from .extract import EXTRACT_MODES_HINT, DataSet, ExtractError, extract, rendering_hint
from .menu import (
    DEFAULT_EXCLUDE,
    DEFAULT_NAV_SELECTORS,
    Menu,
    discover_menus,
    find_page_nav,
    match_menu,
)
from .output import (
    build_payload,
    display_width,
    pad_display,
    render_csv,
    render_heading,
    render_json,
    render_table,
    safe_filename,
)
from .session import RequestError, WebSession

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

PROFILE_SEARCH_DIRS = (Path("profiles"), Path(".websnap"), Path.home() / ".websnap")
AUTO_PROFILE_NAME = "websnap.toml"

EPILOG = """\
例：
  python3 -m websnap                                      交互问答，一步步来
  python3 -m websnap --url http://localhost:8080 --list-menus
  python3 -m websnap --url http://localhost:8080 --username admin --menu 订单中心
  python3 -m websnap --url http://localhost:8080 --menu 订单中心 --format json -o orders.json
  python3 -m websnap --profile profiles/qiandama.toml --menu 5,6 --format csv -o out/
  python3 -m websnap --save-profile profiles/qiandama.toml  把本次自动探测的结果固化下来

密码推荐用交互输入或环境变量 WEBSNAP_PASSWORD，别写在命令行上（会留在 shell 历史里）。
"""


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="websnap",
        description="按菜单抓取 Web 后台数据的通用工具。不带参数运行会进入交互问答。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument("--url", "--base-url", dest="url", help="系统地址，如 http://localhost:8080")
    parser.add_argument("--username", help="登录账号")
    parser.add_argument(
        "--password",
        help="登录密码。不推荐：会出现在 ps 输出和 shell 历史里",
    )
    parser.add_argument(
        "--password-env",
        default="WEBSNAP_PASSWORD",
        help="从哪个环境变量读密码，默认 %(default)s",
    )
    parser.add_argument("--profile", help="站点配置（TOML）路径，或 profiles/ 下的名字")
    parser.add_argument(
        "--menu",
        action="append",
        help="要抓的菜单：序号、名字或路径，可重复，也可逗号分隔",
    )
    parser.add_argument("--list-menus", action="store_true", help="只列出发现的菜单就退出")
    parser.add_argument(
        "--format", choices=("table", "csv", "json"), help="输出格式；给了 -o 且带扩展名可自动推断"
    )
    parser.add_argument("-o", "--out", help="输出文件；多个菜单时视为输出目录")
    parser.add_argument("--timeout", type=float, help="单次请求超时秒数")
    parser.add_argument(
        "--insecure", action="store_true", help="跳过 TLS 证书校验（内网自签证书用）"
    )
    parser.add_argument("--mode", help="提取模式覆盖：" + EXTRACT_MODES_HINT)
    parser.add_argument("--selector", help="限定提取范围的选择器，例如 main 或 #content")
    parser.add_argument(
        "--save-profile", metavar="PATH", help="把本次生效的设置写回一份 TOML 配置"
    )
    parser.add_argument("--version", action="version", version=f"websnap {__version__}")
    parser.add_argument("--selftest", action="store_true", help="跑内置单元测试")
    parser.add_argument(
        "--gui",
        action="store_true",
        help="打开图形界面（等同 python3 -m websnap.gui）",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="配合 --gui：只构建窗口然后退出，用于自检",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    if args.gui:
        # 延迟导入：tkinter 只在真要开窗时才加载，命令行用法不受影响
        from .gui import main as gui_main

        forwarded: list[str] = []
        if args.url:
            forwarded += ["--url", args.url]
        if args.username:
            forwarded += ["--username", args.username]
        if args.password:
            forwarded += ["--password", args.password]
        if args.password_env:
            forwarded += ["--password-env", args.password_env]
        if args.profile:
            forwarded += ["--profile", args.profile]
        if args.insecure:
            forwarded.append("--insecure")
        if args.timeout:
            forwarded += ["--timeout", str(args.timeout)]
        if args.smoke:
            forwarded.append("--smoke")
        return gui_main(forwarded)

    try:
        return run(args)
    except (ConfigError, LoginError, RequestError, ExtractError, SelectorError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


def run_selftest() -> int:
    import unittest

    root = Path(__file__).resolve().parent.parent
    tests = root / "tests"
    if not tests.is_dir():
        print(f"找不到测试目录 {tests}", file=sys.stderr)
        return EXIT_ERROR

    suite = unittest.defaultTestLoader.discover(str(tests), top_level_dir=str(root))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return EXIT_OK if result.wasSuccessful() else EXIT_ERROR


# --------------------------------------------------------------------------- #
# 交互辅助
# --------------------------------------------------------------------------- #

def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{label}{suffix}: ")
    except EOFError as error:
        raise ConfigError(
            "需要交互输入，但标准输入已经结束了。改用命令行参数，或写一份站点配置。"
        ) from error
    answer = answer.strip()
    return answer or (default or "")


def _prompt_secret(label: str) -> str:
    try:
        return getpass.getpass(f"{label}: ")
    except EOFError as error:
        raise ConfigError("需要输入密码，但标准输入已经结束了。") from error


def _print_menus(menus: list[Menu]) -> None:
    width = max((display_width(menu.name) for menu in menus), default=0)
    for index, menu in enumerate(menus, 1):
        print(f"  {index:>2}. {pad_display(menu.name, width)}  {menu.path}")


def _menu_tokens(values: list[str] | None) -> list[str]:
    tokens: list[str] = []
    for value in values or []:
        tokens.extend(part.strip() for part in value.split(",") if part.strip())
    return tokens


def _parse_selection(menus: list[Menu], answer: str) -> list[Menu]:
    selected: list[Menu] = []
    seen: set[str] = set()
    for token in (part.strip() for part in answer.replace("，", ",").split(",")):
        if not token:
            continue
        menu = match_menu(menus, token)
        if menu is None:
            print(f"提醒：没找到匹配 {token!r} 的菜单，已跳过", file=sys.stderr)
            continue
        if menu.path in seen:
            continue
        seen.add(menu.path)
        selected.append(menu)
    return selected


def _resolve_output_target(out: str | None) -> tuple[Path | None, bool]:
    """``-o`` 想指文件还是目录。

    判断依据是"已存在的目录"或"结尾带斜杠"。不能只看后缀——``-o out/``
    这种写法没有任何后缀，但它明显是个目录。
    """
    if not out:
        return None, False
    target = Path(out).expanduser()
    as_dir = target.is_dir() or out.endswith(("/", os.sep))
    return target, as_dir


def _resolve_format(
    explicit: str | None, target: Path | None, as_dir: bool
) -> str:
    if explicit:
        return explicit
    if as_dir:
        # 目录语义天然就是"一个菜单一个文件"，CSV 最合适。
        return "csv"
    if target is not None:
        suffix = target.suffix.lower()
        if suffix == ".json":
            return "json"
        if suffix == ".csv":
            return "csv"
        if suffix in (".txt", ".md", ".log"):
            return "table"
    return "table"


def _resolve_profile(args: argparse.Namespace) -> tuple[Profile, list[str]]:
    if args.profile:
        return load_profile(find_profile(args.profile, PROFILE_SEARCH_DIRS))

    auto = Path(AUTO_PROFILE_NAME)
    if auto.is_file():
        profile, warnings = load_profile(auto)
        warnings.append(f"自动读取了当前目录的 {AUTO_PROFILE_NAME}（用 --profile 可指定别的）")
        return profile, warnings

    return Profile(), []


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def run(args: argparse.Namespace) -> int:
    profile, warnings = _resolve_profile(args)
    for warning in warnings:
        print(f"配置提醒：{warning}", file=sys.stderr)

    interactive = sys.stdin.isatty()
    base_url = args.url or profile.base_url
    if not base_url:
        if not interactive:
            raise ConfigError("没给系统地址。用 --url 指定，或在配置里写 [site] base_url。")
        base_url = _prompt("系统地址", default="http://localhost:8080")
    base_url = base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        base_url = "http://" + base_url

    timeout = args.timeout or profile.timeout
    session = WebSession(
        base_url,
        timeout=timeout,
        headers=profile.extra_headers,
        verify=not (args.insecure or not profile.verify),
    )

    selectors = profile.nav_selectors or DEFAULT_NAV_SELECTORS
    exclude = profile.exclude or DEFAULT_EXCLUDE
    extract_mode = args.mode or profile.extract_mode or "auto"
    extract_selector = args.selector or profile.extract_selector or None

    form = _do_login(args, profile, session, interactive)

    landing = session.get("/")
    landing_tree = parse_html(landing.text)
    menus, nav = discover_menus(
        landing_tree,
        base_url,
        selectors=selectors,
        exclude=exclude,
        min_links=profile.min_links,
    )
    if not menus:
        hint = rendering_hint(landing_tree)
        if hint:
            # 前端渲染的页面，改配置也救不回来，别把人往那个方向引。
            raise ConfigError(f"没能从 {landing.url} 认出菜单（{nav.reason}）。\n{hint}")
        raise ConfigError(
            f"没能从 {landing.url} 认出菜单（{nav.reason}）。"
            "可以写一份站点配置，用 [menus] nav_selectors 指定导航区。"
        )
    print(f"发现 {len(menus)} 个菜单（{nav.reason}）", file=sys.stderr)

    if args.list_menus:
        _print_menus(menus)
        return EXIT_OK

    selected = _select_menus(args, menus, interactive)

    results = []
    for menu in selected:
        response = session.get(menu.path)
        if response.status >= 400:
            print(f"跳过「{menu.name}」：返回 HTTP {response.status}", file=sys.stderr)
            continue

        page_tree = parse_html(response.text)
        excluded = ()
        nav_on_page = find_page_nav(page_tree, selectors, profile.min_links)
        if nav_on_page is not None:
            excluded = (nav_on_page,)

        dataset = extract(
            page_tree,
            base_url,
            selector=extract_selector,
            mode=extract_mode,
            exclude_nodes=excluded,
        )
        results.append((menu, dataset, response.url))
        print(f"抓取「{menu.name}」… {dataset.describe()}", file=sys.stderr)

    if not results:
        raise ExtractError("选中的菜单一个都没抓到数据。")

    _write_output(args, results, profile, base_url)

    if args.save_profile:
        _save(args, profile, base_url, form, nav)

    return EXIT_OK


def _do_login(
    args: argparse.Namespace,
    profile: Profile,
    session: WebSession,
    interactive: bool,
) -> LoginForm | None:
    if profile.auth_mode == "none":
        print("按配置跳过登录。", file=sys.stderr)
        return None

    username = args.username or profile.username
    if not username:
        if not interactive:
            raise ConfigError("没给账号。用 --username 指定，或在配置里写 [auth] username。")
        username = _prompt("登录账号")
    if not username:
        raise ConfigError("账号不能为空。")

    password = args.password or os.environ.get(args.password_env or "")
    if args.password:
        print(
            f"提醒：--password 里的密码会留在 shell 历史和 ps 输出里。"
            f"建议改用交互输入或环境变量 {args.password_env}。",
            file=sys.stderr,
        )
    if not password:
        if not interactive:
            raise ConfigError(
                f"没给密码。设置环境变量 {args.password_env}，或用 --password（不推荐）。"
            )
        password = _prompt_secret("登录密码")
    if not password:
        raise ConfigError("密码不能为空。")

    print(f"正在登录 {session.base_url} …", file=sys.stderr)
    form = login(
        session,
        username,
        password,
        login_path=profile.login_path,
        username_field=profile.username_field,
        password_field=profile.password_field,
    )
    print(f"登录成功（{form.describe()}）", file=sys.stderr)
    return form


def _select_menus(
    args: argparse.Namespace, menus: list[Menu], interactive: bool
) -> list[Menu]:
    tokens = _menu_tokens(args.menu)
    if tokens:
        selected: list[Menu] = []
        seen: set[str] = set()
        for token in tokens:
            menu = match_menu(menus, token)
            if menu is None:
                print(f"提醒：没找到匹配 {token!r} 的菜单，已跳过", file=sys.stderr)
                continue
            if menu.path in seen:
                continue
            seen.add(menu.path)
            selected.append(menu)
        if not selected:
            print("实际发现的菜单：", file=sys.stderr)
            _print_menus(menus)
            raise ConfigError("你指定的菜单一个都没匹配上。")
        return selected

    if not interactive:
        return list(menus)

    _print_menus(menus)
    answer = _prompt("选择菜单（序号或名字，逗号分隔；直接回车 = 全部）", default="")
    if not answer:
        return list(menus)
    selected = _parse_selection(menus, answer)
    if not selected:
        raise ConfigError("没选中任何菜单。")
    return selected


def _render_one(
    output_format: str,
    menu: Menu,
    dataset: DataSet,
    page_url: str,
    site: str,
    stamp: str,
    bom: bool,
) -> str:
    """把一条结果渲染成目标格式的文本。"""
    if output_format == "csv":
        # 写文件加 BOM（Excel 才认中文表头），打终端不加（屏幕上会多出看不见的字符）
        return render_csv(dataset, with_bom=bom)

    if output_format == "json":
        payload = build_payload(
            dataset,
            site=site,
            menu_name=menu.name,
            menu_path=menu.path,
            fetched_at=stamp,
            page_url=page_url,
        )
        return render_json(payload)

    text = render_heading(menu.name, menu.path, dataset) + "\n" + render_table(dataset)
    if dataset.note:
        text += f"\n提示：{dataset.note}"
    return text


def _write_files(
    target_dir: Path, results: list[tuple[Menu, DataSet, str]], output_format: str,
    site: str, stamp: str,
) -> None:
    """一个菜单一个文件。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = {"csv": ".csv", "json": ".json", "table": ".txt"}[output_format]

    used: set[str] = set()
    for menu, dataset, page_url in results:
        stem = safe_filename(menu.name or menu.path)
        if stem in used:
            stem = f"{stem}_{len(used) + 1}"
        used.add(stem)

        path = target_dir / (stem + suffix)
        text = _render_one(output_format, menu, dataset, page_url, site, stamp, bom=True)
        path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
        print(f"已写入 {path}（{dataset.count} 条）", file=sys.stderr)


def _combined_payload(
    results: list[tuple[Menu, DataSet, str]], site: str, stamp: str
) -> dict:
    """多个菜单合成一个 JSON 文件时的结构。"""
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
    return {
        "site": site,
        "fetched_at": stamp,
        "menus": len(payloads),
        "results": payloads,
    }


def _write_output(
    args: argparse.Namespace,
    results: list[tuple[Menu, DataSet, str]],
    profile: Profile,
    base_url: str,
) -> None:
    target, as_dir = _resolve_output_target(args.out)
    output_format = _resolve_format(args.format, target, as_dir)
    site = profile.name or base_url
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")

    # 目录语义：一个菜单一个文件。
    if as_dir:
        assert target is not None  # as_dir 为真时 target 必然存在
        _write_files(target, results, output_format, site, stamp)
        return

    # 文件语义。JSON 可以把多个菜单装进一个文件（结构是 results 数组），
    # CSV 和表格不行——往同一个文件里追加两次，后一次会盖掉前一次，而且
    # 不报错。这种坑当下看不出来，等发现时数据已经没了。
    if target is not None and len(results) > 1 and output_format != "json":
        raise ConfigError(
            f"--out 给的是单个文件（{target}），但有 {len(results)} 个菜单。"
            "改成给一个目录（结尾带 /）就能一个菜单一个文件。"
        )

    if target is None:
        if output_format == "csv":
            for menu, dataset, page_url in results:
                print(render_heading(menu.name, menu.path, dataset))
                print(render_csv(dataset, with_bom=False), end="")
                print()
        elif output_format == "json" and len(results) > 1:
            print(render_json(_combined_payload(results, site, stamp)))
        elif output_format == "table" and len(results) > 1:
            # 终端下每个菜单一段，段间空行分隔。之前这里只渲染了 results[0]，
            # 抓了 9 个菜单却只显示第一个，且不报任何错。
            for menu, dataset, page_url in results:
                print(_render_one("table", menu, dataset, page_url, site, stamp, bom=False))
                print()
        else:
            print(_render_one(output_format, *results[0], site, stamp, bom=False))
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json" and len(results) > 1:
        text = render_json(_combined_payload(results, site, stamp))
    else:
        text = _render_one(output_format, *results[0], site, stamp, bom=True)
    target.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
    total = sum(dataset.count for _, dataset, _ in results)
    print(f"已写入 {target}（{len(results)} 个菜单，共 {total} 条）", file=sys.stderr)


def _save(
    args: argparse.Namespace,
    profile: Profile,
    base_url: str,
    form: LoginForm | None,
    nav,
) -> None:
    profile.base_url = base_url
    profile.verify = not args.insecure
    # 账号存下来（方便下次直接用），密码不存——配置会进版本库，凭据不会。
    profile.username = args.username or profile.username or None
    if not profile.name:
        profile.name = urllib.parse.urlsplit(base_url).netloc
    if args.timeout:
        profile.timeout = args.timeout
    if args.mode:
        profile.extract_mode = args.mode
    if args.selector:
        profile.extract_selector = args.selector

    if form is not None:
        path = urllib.parse.urlparse(form.login_page_url).path
        if path:
            profile.login_path = path
        profile.username_field = form.username_field
        profile.password_field = form.password_field

    if nav is not None and nav.selector:
        profile.nav_selectors = (nav.selector,)

    location = save_profile(args.save_profile, profile)
    print(f"已把本次生效的设置写入 {location}", file=sys.stderr)
