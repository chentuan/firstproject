"""websnap —— 按菜单抓取 Web 后台数据的通用工具。

设计前提：**引擎通用，配置逐系统**。

* 通用引擎：会话与登录探测、菜单发现、页面结构识别、输出。写一次，所有系统共用。
* 站点配置：登录字段、导航选择器、提取规则。换系统写一份 TOML，不改代码。

自动探测优先，配置兜底。能被自动认出来的东西不该要求人去配。
"""

from .auth import LoginError, LoginForm, login, parse_login_form
from .config import ConfigError, Profile, load_profile, save_profile
from .dom import Node, SelectorError, parse_html, select
from .extract import ExtractError, DataSet, extract
from .menu import Menu, discover_menus
from .session import RequestError, WebSession

__version__ = "0.1.0"

__all__ = [
    "ConfigError",
    "DataSet",
    "ExtractError",
    "LoginError",
    "LoginForm",
    "Menu",
    "Node",
    "Profile",
    "RequestError",
    "SelectorError",
    "WebSession",
    "__version__",
    "discover_menus",
    "extract",
    "load_profile",
    "login",
    "parse_html",
    "parse_login_form",
    "save_profile",
    "select",
]
