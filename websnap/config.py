"""站点配置：每个系统一份 TOML，换系统只改配置不改代码。

**为什么不是 YAML。** PyYAML 不是标准库。为了保住"零依赖"这个卖点去手写一个
YAML 子集解析器不划算；JSON 倒是标准库，但 JSON 不能写注释——而配置文件不能
写注释，等于逼着下一个接手的人去读代码猜每个字段什么含义。

TOML 两头都占：``tomllib`` 从 Python 3.11 起就在标准库里，而且允许注释。
本机是 3.13，没有理由不用。

配置文件的定位是**兜底而不是必需品**：自动探测能跑通就不用写它。真跑不通、
或者探测结果不稳定时，才把结论固化下来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:  # tomllib 从 Python 3.11 起进入标准库
    import tomllib
except ModuleNotFoundError as error:  # pragma: no cover - 低版本 Python 兜底
    raise SystemExit(
        "需要 Python 3.11 或更高版本（站点配置用标准库 tomllib 读 TOML）。"
    ) from error


class ConfigError(RuntimeError):
    """配置有问题。消息直接给人看。"""


AUTH_MODES = ("auto", "none")
EXTRACT_MODES = ("auto", "table", "list", "cards", "kv", "text")

# 每个配置节里认识的键。写错一个字母就静默失效，是最难查的 bug，
# 所以遇到不认识的键要出声。
_KNOWN_KEYS: dict[str, set[str]] = {
    "site": {"name", "base_url", "timeout", "verify"},
    "auth": {
        "mode",
        "login_path",
        "username",
        "username_field",
        "password_field",
        "extra_headers",
    },
    "menus": {"nav_selectors", "exclude", "min_links"},
    "extract": {"mode", "selector"},
}


@dataclass
class Profile:
    """一份站点配置。所有字段都有默认值——空配置也是合法配置。"""

    name: str = ""
    base_url: str = ""
    timeout: float = 15.0
    verify: bool = True

    auth_mode: str = "auto"
    login_path: str | None = None
    username: str | None = None
    username_field: str | None = None
    password_field: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    nav_selectors: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    min_links: int = 3

    extract_mode: str = "auto"
    extract_selector: str = ""

    source: str | None = None


def _as_str_tuple(value: object, key: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ConfigError(f"{key} 需要字符串或字符串数组，收到 {type(value).__name__}")


def _as_str_dict(value: object, key: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(f"{key} 需要一张键值表，收到 {type(value).__name__}")
    return {str(k): str(v) for k, v in value.items()}


def load_profile(path: str | Path) -> tuple[Profile, list[str]]:
    """读一份 TOML 配置，返回 ``(配置, 警告列表)``。"""
    location = Path(path).expanduser()
    if not location.is_file():
        raise ConfigError(f"找不到配置文件：{location}")

    try:
        raw = tomllib.loads(location.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{location} 不是合法的 TOML：{error}") from error

    warnings: list[str] = []
    for section, keys in raw.items():
        if section not in _KNOWN_KEYS:
            warnings.append(f"配置里有个不认识的节 [{section}]，已忽略")
            continue
        if not isinstance(keys, dict):
            warnings.append(f"[{section}] 应该是一张表，已忽略")
            continue
        for key in keys:
            if key not in _KNOWN_KEYS[section]:
                warnings.append(
                    f"[{section}] 里有个不认识的键 {key!r}，已忽略（是不是拼错了？）"
                )

    site = raw.get("site", {}) or {}
    auth = raw.get("auth", {}) or {}
    menus = raw.get("menus", {}) or {}
    extract_section = raw.get("extract", {}) or {}

    if not all(isinstance(section, dict) for section in (site, auth, menus, extract_section)):
        raise ConfigError("配置文件里的节必须都是表（[site] 这种写法）")

    profile = Profile(source=str(location))

    profile.name = str(site.get("name", "") or "")
    profile.base_url = str(site.get("base_url", "") or "")
    if "timeout" in site:
        try:
            profile.timeout = float(site["timeout"])
        except (TypeError, ValueError) as error:
            raise ConfigError(f"[site] timeout 要是数字，收到 {site['timeout']!r}") from error
    if "verify" in site:
        profile.verify = bool(site["verify"])

    profile.auth_mode = str(auth.get("mode", "auto") or "auto")
    if profile.auth_mode not in AUTH_MODES:
        raise ConfigError(f"[auth] mode 只能是 {AUTH_MODES}，收到 {profile.auth_mode!r}")
    profile.login_path = auth.get("login_path") or None
    profile.username = auth.get("username") or None
    profile.username_field = auth.get("username_field") or None
    profile.password_field = auth.get("password_field") or None
    if "extra_headers" in auth:
        profile.extra_headers = _as_str_dict(auth["extra_headers"], "[auth] extra_headers")

    if "nav_selectors" in menus:
        profile.nav_selectors = _as_str_tuple(menus["nav_selectors"], "[menus] nav_selectors")
    if "exclude" in menus:
        profile.exclude = _as_str_tuple(menus["exclude"], "[menus] exclude")
    if "min_links" in menus:
        try:
            profile.min_links = int(menus["min_links"])
        except (TypeError, ValueError) as error:
            raise ConfigError(f"[menus] min_links 要是整数，收到 {menus['min_links']!r}") from error

    profile.extract_mode = str(extract_section.get("mode", "auto") or "auto")
    if profile.extract_mode not in EXTRACT_MODES:
        raise ConfigError(
            f"[extract] mode 只能是 {EXTRACT_MODES}，收到 {profile.extract_mode!r}"
        )
    profile.extract_selector = str(extract_section.get("selector", "") or "")

    return profile, warnings


def save_profile(path: str | Path, profile: Profile) -> Path:
    """把当前生效的设置写成一份带注释的 TOML。

    这是自动探测的收口：先零配置跑一次，跑通了把它固化下来，下次就不用再赌
    探测结果。**密码不写进文件**——配置进版本库是常态，凭据进版本库是事故。
    """
    location = Path(path).expanduser()
    location.parent.mkdir(parents=True, exist_ok=True)

    selectors = profile.nav_selectors or ()
    exclude = profile.exclude or ()

    def string_array(values: tuple[str, ...]) -> str:
        if not values:
            return "[]"
        return "[" + ", ".join(f'"{value}"' for value in values) + "]"

    lines = [
        f"# websnap 站点配置：{profile.name or profile.base_url or '未命名'}",
        "#",
        "# 自动探测跑得通就不需要这个文件。它的用途是：把探测结果固化下来，",
        "# 以及在探测不准的时候人工纠正。密码故意不写在这里——配置会进版本库。",
        "",
        "[site]",
        f'name = "{profile.name or ""}"',
        f'base_url = "{profile.base_url}"',
        f"timeout = {profile.timeout}",
        f"verify = {str(profile.verify).lower()}",
        "",
        "[auth]",
        "# auto = 自动探测登录表单；none = 站点不需要登录",
        f'mode = "{profile.auth_mode}"',
    ]
    if profile.login_path:
        lines.append(f'login_path = "{profile.login_path}"')
    if profile.username:
        lines.append(f'username = "{profile.username}"')
    lines.append("# 探测结果，一般不用改")
    if profile.username_field:
        lines.append(f'username_field = "{profile.username_field}"')
    if profile.password_field:
        lines.append(f'password_field = "{profile.password_field}"')
    if profile.extra_headers:
        entries = ", ".join(f'{k} = "{v}"' for k, v in profile.extra_headers.items())
        lines.append(f"extra_headers = {{ {entries} }}")

    lines += [
        "",
        "[menus]",
        "# 留空 = 用内置的语义选择器自动找导航区；写上则只用这里列的",
        f"nav_selectors = {string_array(selectors)}",
        "# 留空 = 用内置默认排除词（logout / 退出 / 注销 等）",
        f"exclude = {string_array(exclude)}",
        f"min_links = {profile.min_links}",
        "",
        "[extract]",
        "# auto | table | list | cards | kv | text",
        f'mode = "{profile.extract_mode}"',
        "# 留空表示整个页面；写选择器可以把范围限定到内容区，比如 main 或 #content",
        f'selector = "{profile.extract_selector}"',
        "",
    ]

    location.write_text("\n".join(lines), encoding="utf-8")
    return location


def find_profile(name_or_path: str, search_dirs: tuple[Path, ...] = ()) -> Path:
    """按路径或名字找配置。``--profile qiandama`` 也能用。"""
    candidate = Path(name_or_path).expanduser()
    if candidate.is_file():
        return candidate

    for directory in search_dirs:
        for pattern in (f"{name_or_path}.toml", name_or_path):
            found = directory / pattern
            if found.is_file():
                return found

    raise ConfigError(
        f"找不到配置 {name_or_path!r}。给个文件路径，或者把它放到 profiles/ 下。"
    )
