"""菜单发现：不写死菜单表，从页面上自己认出来。

这是"通用"里最容易被低估的一环。写死 ``{"订单中心": "/orders", ...}`` 五分钟
就能跑通一个系统，然后换个系统就废了——而且系统加一个菜单，脚本就落后一版。

这里的做法分两级：

1. **语义优先。** 页面上有 ``<nav>`` / ``<aside>`` / ``[role=navigation]`` /
   ``.sidebar`` 这类明确表示"这是导航"的容器，直接用。
2. **链接密度兜底。** 没有语义标记时，按"链接文本占该容器总文本的比例"打分。
   导航区的链接密度接近 1（整块都是链接文字），正文区则低得多。这是区分
   "菜单"和"文章里恰好有几个链接"的关键信号。
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

from .dom import Node, select

# 表示"这里是导航"的选择器，按可信度从高到低。
DEFAULT_NAV_SELECTORS: tuple[str, ...] = (
    "nav",
    "aside",
    "[role=navigation]",
    ".sidebar",
    ".side-bar",
    ".sidenav",
    ".side-nav",
    ".navbar",
    ".topnav",
    ".top-nav",
    ".main-nav",
    ".mainmenu",
    ".main-menu",
    ".nav",
    ".menu",
    ".left-menu",
    "#sidebar",
    "#side-menu",
    "#menu",
    "#nav",
    "#navigation",
    "header",
)

# 这些不该当成菜单项。按路径和锚文本双查。
DEFAULT_EXCLUDE: tuple[str, ...] = (
    "logout",
    "signout",
    "sign-out",
    "logoff",
    "exit",
    "注销",
    "退出",
    "登出",
)

# 锚文本超过这个长度，基本是正文里的链接而不是菜单项。
MAX_MENU_TEXT = 24

_DEAD_SCHEMES = ("javascript:", "mailto:", "tel:", "blob:", "data:", "about:")


@dataclass
class Menu:
    """一个菜单项。``path`` 是推导出的绝对地址。"""

    name: str
    path: str
    href: str

    def __str__(self) -> str:
        return f"{self.name}  {self.path}"


@dataclass
class NavMatch:
    """导航区的判定结果。带上依据，出错时才知道该调哪里。"""

    container: Node | None
    reason: str
    selector: str | None = None

    @property
    def found(self) -> bool:
        return self.container is not None


def _anchor_text(anchor: Node) -> str:
    """菜单名。取锚文本，折叠空白。

    空文本时退到 ``title`` / ``aria-label``——图标菜单（只有 ``<i class="icon">``）
    很常见，这种时候锚文本是空的，只能看这两个属性。
    """
    text = anchor.clean_text()
    if text:
        return text
    for attribute in ("title", "aria-label"):
        value = anchor.get(attribute)
        if value and value.strip():
            return " ".join(value.split())
    return ""


def _link_density(container: Node, anchors: list[Node]) -> float:
    """容器里"链接文本"占"全部文本"的比例。导航区接近 1。"""
    total = len(container.clean_text())
    if total == 0:
        return 0.0
    link_chars = sum(len(anchor.clean_text()) for anchor in anchors)
    return min(1.0, link_chars / total)


def _container_anchors(container: Node) -> list[Node]:
    return [node for node in container.elements("a") if node.get("href")]


def _depth(node: Node) -> int:
    return sum(1 for _ in node.ancestors())


def find_nav_container(
    tree: Node,
    selectors: tuple[str, ...] = DEFAULT_NAV_SELECTORS,
    min_links: int = 3,
) -> NavMatch:
    """找出承载菜单的容器。"""
    # 第一级：语义选择器。取命中里链接最多的那个。
    best_semantic: tuple[int, int, str, Node] | None = None
    for selector in selectors:
        for container in select(tree, selector):
            anchors = _container_anchors(container)
            if len(anchors) < min_links:
                continue
            # 链接多的优先；链接数相同时取更靠外的（深度小）——外层容器才包含
            # 完整菜单，内层的往往只是其中一个子菜单。
            key = (len(anchors), -_depth(container), selector, container)
            if best_semantic is None or key[:2] > best_semantic[:2]:
                best_semantic = key

    if best_semantic is not None:
        count, _, selector, container = best_semantic
        return NavMatch(container, f"语义标记 {selector!r}，{count} 个链接", selector)

    # 第二级：链接密度。
    best_scored: tuple[float, int, int, Node] | None = None
    for node in tree.elements():
        if node.tag in ("html", "body", "head"):
            continue
        anchors = _container_anchors(node)
        if len(anchors) < min_links:
            continue
        density = _link_density(node, anchors)
        if density < 0.5:
            continue
        key = (density, len(anchors), -_depth(node), node)
        if best_scored is None or key[:3] > best_scored[:3]:
            best_scored = key

    if best_scored is not None:
        density, count, _, container = best_scored
        return NavMatch(
            container, f"链接密度 {density:.0%}（{count} 个链接），无语义标记", None
        )

    return NavMatch(None, "页面上没找到像导航的区域", None)


def discover_menus(
    tree: Node,
    base_url: str,
    selectors: tuple[str, ...] = DEFAULT_NAV_SELECTORS,
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE,
    min_links: int = 3,
) -> tuple[list[Menu], NavMatch]:
    """从页面里认出菜单列表。返回 ``(菜单, 导航区判定结果)``。"""
    match = find_nav_container(tree, selectors, min_links)
    if match.container is None:
        return [], match

    menus: list[Menu] = []
    seen: set[str] = set()

    for anchor in _container_anchors(match.container):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        if href.lower().startswith(_DEAD_SCHEMES):
            continue

        name = _anchor_text(anchor)
        if not name or len(name) > MAX_MENU_TEXT:
            continue

        haystack = f"{name} {href}".lower()
        if any(token in haystack for token in exclude):
            continue

        absolute = urllib.parse.urljoin(base_url.rstrip("/") + "/", href)
        # 去掉 fragment 再比对：`/orders#top` 和 `/orders` 是同一个菜单。
        key = urllib.parse.urlsplit(absolute)._replace(fragment="").geturl()
        if key in seen:
            continue
        seen.add(key)

        menus.append(Menu(name=name, path=key, href=href))

    if not menus:
        match.reason += "，但里面没有可用的菜单项"
        return [], match

    return menus, match


def find_page_nav(
    tree: Node,
    selectors: tuple[str, ...] = DEFAULT_NAV_SELECTORS,
    min_links: int = 3,
) -> Node | None:
    """在内容页上再认一次导航区，用于把它排除在数据提取范围之外。

    只在「语义标记命中 **且** 链接密度够高」时才返回。这个保守是故意的：
    误把数据区当成导航而排除掉，比漏排除（多抓几条菜单文字进来）严重得多。
    """
    match = find_nav_container(tree, selectors, min_links)
    if match.container is None or match.selector is None:
        return None
    anchors = _container_anchors(match.container)
    if _link_density(match.container, anchors) < 0.5:
        return None
    return match.container


def match_menu(menus: list[Menu], token: str) -> Menu | None:
    """按名字、路径或 ``#序号`` 找一个菜单。找不到返回 ``None``。"""
    token = token.strip()
    if not token:
        return None

    if token.startswith("#"):
        token = token[1:]

    if token.isdigit():
        index = int(token) - 1
        if 0 <= index < len(menus):
            return menus[index]

    lowered = token.lower()
    for menu in menus:
        if menu.name == token or menu.path == token:
            return menu
    for menu in menus:
        if lowered in menu.name.lower() or lowered in menu.path.lower():
            return menu
    return None
