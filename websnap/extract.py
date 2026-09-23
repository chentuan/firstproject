"""通用数据提取：不预设结构，先看页面上有什么。

最容易走错的路是"给每个菜单写一个解析规则"。那样九个菜单就是九份代码，而且
后台一改模板就全废。

这里的做法是**识别结构类型**，而不是识别具体字段：

* ``<table>`` 有 ``thead`` → 表头就是列名，一行一条记录。列名从页面读，
  所以九个菜单共用一套代码也能各自取到正确的列。
* 没有 ``thead`` 但只有两列、行数 ≥ 2 → 大概率是"标签 / 值"的详情页。
* ``<ul>`` / ``<ol>`` 里 ≥ 3 个同构 ``<li>`` → 列表。
* 一个容器里 ≥ 3 个同构子元素 → 卡片列表。
* ``<dl>`` → 键值对。
* 以上都不成立 → 退化成正文文本，一行一条，别假装看懂了。

单元格里的链接不会污染列名，而是收进该行的 ``_links`` 字段。
"""

from __future__ import annotations

import re
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

from .dom import NON_CONTENT_TAGS, Node, select

TABLE = "table"
LIST = "list"
KV = "kv"
CARDS = "cards"
TEXT = "text"

# 给命令行 --help 用的说明文本，跟上面的常量保持一处定义。
EXTRACT_MODES_HINT = "auto|table|list|cards|kv|text"

# 文本兜底模式最多输出多少行，防止把一个几万字的页面整个吐出来。
TEXT_ROW_LIMIT = 300

# 少于这个行数的表不算数据表（避免把布局用的表格当成数据）。
MIN_TABLE_ROWS = 1
MIN_LIST_ITEMS = 3
MIN_CARD_ITEMS = 3

# 表头为空时，如果这一列的数据里带链接，就说明它是"操作"列。
ACTION_COLUMN = "操作"


class ExtractError(RuntimeError):
    """提取失败。消息直接给人看。"""


@dataclass
class DataSet:
    """一次提取的结果。``kind`` 说明它是什么结构。"""

    kind: str
    columns: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def count(self) -> int:
        return len(self.rows)

    def describe(self) -> str:
        labels = {"table": "表格", "list": "列表", "kv": "键值对", "cards": "卡片列表", "text": "文本"}
        return f"{labels.get(self.kind, self.kind)}，{self.count} 条"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _depth(node: Node) -> int:
    return sum(1 for _ in node.ancestors())


def _descendants(scope: Node, *tags: str) -> Iterator[Node]:
    """元素遍历，**包含 scope 自身**。

    ``Node.elements()`` 不含自身。配上 ``--selector`` 就会出问题：选择器写
    ``table.data`` 这种"直接指着数据容器"的形式时，容器是 scope 本身，于是
    一条都找不到，还报"页面上没找到数据表格"——这个坑必须堵住。
    """
    for node in scope.self_and_walk():
        if node.tag is not None and (not tags or node.tag in tags):
            yield node


def _is_excluded(node: Node, excluded: Sequence[Node]) -> bool:
    """节点是否落在被排除的子树里（导航容器之类）。"""
    for target in excluded:
        if node is target:
            return True
        for ancestor in node.ancestors():
            if ancestor is target:
                return True
    return False


def _unique(names: Iterable[str]) -> list[str]:
    """列名去重。

    行是 dict，列名重复会直接互相覆盖——"状态"出现两次就丢一列数据，
    而且丢得毫无提示。必须在这里堵住。
    """
    counts: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        if name in counts:
            counts[name] += 1
            out.append(f"{name}_{counts[name]}")
        else:
            counts[name] = 0
            out.append(name)
    return out


def _first_href(node: Node) -> str | None:
    for anchor in node.elements("a"):
        href = anchor.get("href")
        if href and href.strip():
            return href.strip()
    return None


# --------------------------------------------------------------------------- #
# 表格
# --------------------------------------------------------------------------- #

def _iter_table_rows(table: Node) -> Iterator[tuple[str, Node]]:
    """按文档顺序产出 ``(所属分区, tr)``。

    浏览器会在 ``<table>`` 和 ``<tr>`` 之间自动补 ``<tbody>``，但手工写的模板
    常常不补，所以两种情况都得认。
    """

    def walk(node: Node, section: str) -> Iterator[tuple[str, Node]]:
        for child in node.children:
            if child.tag == "tr":
                yield section, child
            elif child.tag in ("thead", "tbody", "tfoot"):
                yield from walk(child, child.tag)

    yield from walk(table, "tbody")


def _cells(tr: Node) -> list[Node]:
    return [child for child in tr.children if child.tag in ("td", "th")]


def _cell_span(cell: Node) -> int:
    raw = (cell.get("colspan") or "1").strip()
    try:
        span = int(raw)
    except ValueError:
        return 1
    return span if span > 1 else 1


def _expand_cells(cells: list[Node]) -> list[Node]:
    """按 ``colspan`` 展开。合并格的内容重复填满它占的每一列。

    表头和数据行都要展开，否则带合并单元格的表——这在后台里非常常见——
    列名数和数据列数就对不上，整张表会被判成"列数不符"全部跳过。
    """
    expanded: list[Node] = []
    for cell in cells:
        expanded.extend([cell] * _cell_span(cell))
    return expanded


def _table_to_dataset(
    table: Node, base_url: str, notes: list[str]
) -> DataSet | None:
    """把一张表转成 DataSet。判断不出是数据表就返回 ``None``。"""
    rows = list(_iter_table_rows(table))
    if not rows:
        return None

    head_rows = [tr for section, tr in rows if section == "thead"]
    body_rows = [tr for section, tr in rows if section != "thead"]

    header_cells: list[Node] | None = None

    if head_rows:
        header_cells = _expand_cells(_cells(head_rows[0]))
        if len(head_rows) > 1:
            notes.append("表格有多个表头行（多级表头），只取了第一行")
    elif body_rows:
        first = _cells(body_rows[0])
        # 没写 thead，但首行全是 th，那也是表头。
        if first and all(cell.tag == "th" for cell in first):
            header_cells = _expand_cells(first)
            body_rows = body_rows[1:]

    # 展开数据行
    expanded: list[list[Node]] = []
    for tr in body_rows:
        cells = _cells(tr)
        if not cells:
            continue
        expanded.append(_expand_cells(cells))

    # --- 键值对形态：无表头、恰好两列、至少两行 ---
    if header_cells is None and expanded and all(len(row) == 2 for row in expanded):
        if len(expanded) >= 2:
            records = [
                {"字段": row[0].clean_text(), "值": row[1].clean_text()}
                for row in expanded
            ]
            return DataSet(KV, ["字段", "值"], records)

    width = len(header_cells) if header_cells else max((len(r) for r in expanded), default=0)
    if width == 0:
        return None
    if width == 1 and header_cells is None and len(expanded) < 2:
        # 单列单行的表，多半是布局用的。
        return None

    # --- 列名 ---
    if header_cells:
        names = [(cell.clean_text() or "").replace("\n", " ") for cell in header_cells]
        if len(names) < width:
            names.extend([""] * (width - len(names)))
    else:
        names = [""] * width

    for index, name in enumerate(names):
        if name:
            continue
        # 表头是空的：如果这一列的数据里全是链接，它就是操作列。
        has_link = any(
            index < len(row) and _first_href(row[index]) for row in expanded
        )
        names[index] = ACTION_COLUMN if has_link else f"列{index + 1}"

    names = _unique(names)

    records: list[dict[str, Any]] = []
    skipped = 0
    for row in expanded:
        if len(row) != len(names):
            # 列数对不上宁可跳过，也不能把数据错位塞进去——错位的数据比
            # 缺失的数据危险得多，因为看不出来。
            skipped += 1
            continue
        record: dict[str, Any] = {}
        links: dict[str, str] = {}
        for name, cell in zip(names, row):
            record[name] = cell.clean_text()
            href = _first_href(cell)
            if href:
                links[name] = urllib.parse.urljoin(base_url.rstrip("/") + "/", href)
        if links:
            record["_links"] = links
        records.append(record)

    if skipped:
        notes.append(f"有 {skipped} 行因列数与表头不符被跳过")
    if any(_cell_span(cell) > 1 for row in [header_cells or []] for cell in row):
        notes.append("表头里有合并单元格，同名列表头可能是展开出来的")

    if not records and header_cells is None:
        return None

    return DataSet(TABLE, names, records)


def _table_candidates(
    scope: Node,
    base_url: str,
    notes: list[str],
    exclude_nodes: Sequence[Node] = (),
) -> list[tuple[float, DataSet, Node]]:
    """页面上所有候选表格，带评分和来源节点。"""
    found: list[tuple[float, DataSet, Node]] = []
    for table in _descendants(scope, "table"):
        if _is_excluded(table, exclude_nodes):
            continue
        local_notes: list[str] = []
        dataset = _table_to_dataset(table, base_url, local_notes)
        if dataset is None:
            continue
        # 行多的优先，列多的其次；有明确表头的再加权——键值对不算。
        score = dataset.count * max(1, len(dataset.columns))
        if dataset.kind == TABLE:
            score *= 1.5
        notes.extend(local_notes)
        found.append((score, dataset, table))
    return found


# --------------------------------------------------------------------------- #
# 列表 / 卡片 / 键值对
# --------------------------------------------------------------------------- #

def _list_dataset(
    scope: Node, base_url: str, exclude_nodes: Sequence[Node] = ()
) -> DataSet | None:
    best: tuple[int, Node, list[Node]] | None = None

    for node in _descendants(scope, "ul", "ol"):
        if _is_excluded(node, exclude_nodes):
            continue
        items = [child for child in node.children if child.tag == "li"]
        if len(items) < MIN_LIST_ITEMS:
            continue
        # 有内容的条目要占多数，否则多半只是个装饰性列表。
        texts = [item.clean_text(("ul", "ol")) for item in items]
        if sum(1 for text in texts if text) < len(items) * 0.6:
            continue
        if best is None or len(items) > best[0]:
            best = (len(items), node, items)

    if best is None:
        return None

    _, _, items = best
    records: list[dict[str, Any]] = []
    for item in items:
        # 嵌套的子列表不算这一条的内容，否则父菜单会把子菜单的文字全吸进来。
        text = item.clean_text(("ul", "ol"))
        record: dict[str, Any] = {"内容": text}
        href = _first_href(item)
        if href:
            record["_links"] = {"内容": urllib.parse.urljoin(base_url.rstrip("/") + "/", href)}
        records.append(record)

    return DataSet(LIST, ["内容"], records)


def _leaf_texts(node: Node) -> list[str]:
    """收集"末端"元素的文本，也就是没有元素子节点、只装文字的那些。"""
    out: list[str] = []
    for child in node.children:
        if child.tag is None or child.tag in NON_CONTENT_TAGS:
            continue
        # 判断"末端"时要跳过 script/style 这类空壳，否则一个卡片里挂个脚本
        # 就会被误判成"还有子元素"，字段名全丢。
        if any(grand.tag is not None and grand.tag not in NON_CONTENT_TAGS for grand in child.walk()):
            out.extend(_leaf_texts(child))
        else:
            text = child.clean_text()
            if text:
                out.append(text)
    return out


def _cards_dataset(
    scope: Node, base_url: str, exclude_nodes: Sequence[Node] = ()
) -> DataSet | None:
    """容器里 ≥ 3 个同构子元素 → 卡片列表。"""
    best: tuple[tuple[int, int], Node, list[Node]] | None = None

    for node in _descendants(scope):
        if node.tag in ("html", "body", "head"):
            continue
        if _is_excluded(node, exclude_nodes):
            continue
        children = [
            child
            for child in node.children
            if child.tag is not None and child.tag not in NON_CONTENT_TAGS
        ]
        if len(children) < MIN_CARD_ITEMS:
            continue

        signature, count = Counter((c.tag, c.classes) for c in children).most_common(1)[0]
        if count < MIN_CARD_ITEMS or count / len(children) < 0.8:
            continue

        cards = [c for c in children if (c.tag, c.classes) == signature]
        if sum(1 for c in cards if c.clean_text()) < count * 0.6:
            continue

        # 卡片多的优先，同样多时取更靠里的（更具体的容器）。
        key = (count, _depth(node))
        if best is None or key > best[0]:
            best = (key, node, cards)

    if best is None:
        return None

    _, _, cards = best
    chunks = [_leaf_texts(card) for card in cards]

    if chunks and chunks[0] and len({len(chunk) for chunk in chunks}) == 1:
        columns = [f"字段{index + 1}" for index in range(len(chunks[0]))]
        records = [dict(zip(columns, chunk)) for chunk in chunks]
        note = "卡片里没找到可靠的字段名，列名是占位符；可用站点配置的 [extract] selector 精确指定"
        return DataSet(CARDS, columns, records, note)

    records = [{"内容": card.clean_text()} for card in cards]
    return DataSet(CARDS, ["内容"], records)


def _dl_dataset(scope: Node, exclude_nodes: Sequence[Node] = ()) -> DataSet | None:
    for node in _descendants(scope, "dl"):
        if _is_excluded(node, exclude_nodes):
            continue
        terms = [child for child in node.children if child.tag == "dt"]
        values = [child for child in node.children if child.tag == "dd"]
        if len(terms) >= 2 and len(terms) == len(values):
            records = [
                {"字段": term.clean_text(), "值": value.clean_text()}
                for term, value in zip(terms, values)
            ]
            return DataSet(KV, ["字段", "值"], records)
    return None


def _text_dataset(scope: Node, exclude_nodes: Sequence[Node] = ()) -> DataSet:
    """兜底：把页面上所有末端块级文本按顺序列出来。"""
    blocks = ("p", "div", "li", "td", "th", "dd", "dt", "span", "h1", "h2", "h3", "h4", "h5", "h6", "label")
    lines: list[str] = []
    seen: set[str] = set()

    for node in _descendants(scope, *blocks):
        if any(child.tag is not None for child in node.children):
            continue
        if _is_excluded(node, exclude_nodes):
            continue
        text = node.clean_text()
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(text)
        if len(lines) >= TEXT_ROW_LIMIT:
            break

    records = [{"内容": line} for line in lines]
    note = ""
    if len(lines) >= TEXT_ROW_LIMIT:
        note = f"只输出了前 {TEXT_ROW_LIMIT} 行"
    return DataSet(TEXT, ["内容"], records, note)


# --------------------------------------------------------------------------- #
# 前端渲染识别
# --------------------------------------------------------------------------- #

# 单页应用常见的挂载点 id。
_APP_MOUNT_IDS = ("app", "root", "main", "mount")

# 打包产物的文件名特征。
_BUNDLE_HINTS = (
    "chunk-vendors",
    "chunk-common",
    "runtime.",
    "vendors.",
    "polyfills.",
    "/static/js/",
    "bundle.js",
)

# webpack 给产物加的内容哈希，如 app.31165e54.js
_BUNDLE_HASH_RE = re.compile(r"/[a-z][\w.-]*\.[0-9a-f]{8}\.js$", re.I)


def _looks_like_bundle(src: str) -> bool:
    lowered = src.lower().split("?", 1)[0]
    if not lowered.endswith(".js"):
        return False
    if any(hint in lowered for hint in _BUNDLE_HINTS):
        return True
    return bool(_BUNDLE_HASH_RE.search(lowered))


def detect_client_rendered(tree: Node) -> list[str]:
    """页面疑似由前端 JS 渲染时返回证据列表；否则返回空列表。

    websnap 解析的是服务端返回的 HTML。Vue / React 这类单页应用给的 HTML 只是
    个空壳，数据要等 JS 跑完才出现。这时"0 条数据"跟"这一页本来就没数据"是两码事：
    后者调调 selector 也许有救，前者换任何配置都抓不到，得换技术路线（headless
    浏览器，或者去逆向它的 XHR 接口）。

    所以宁可多说一句，也不要让人对着空结果反复改配置。
    """
    body = tree.find("body")
    if body is None:
        return []

    # 页面上本来就有大段文字，就不是空壳。
    visible = body.clean_text().strip()
    if len(visible) > 200:
        return []

    evidence: list[str] = []

    for node in _descendants(body, "div", "section", "main"):
        ident = (node.get("id") or "").strip().lower()
        if ident in _APP_MOUNT_IDS and not node.clean_text().strip():
            evidence.append(f'body 里只有一个空的 <{node.tag} id="{ident}">，内容还没被填充')
            break

    bundles = [
        src
        for src in (node.get("src") or "" for node in tree.elements("script"))
        if _looks_like_bundle(src)
    ]
    if bundles:
        evidence.append(f"引用了前端打包产物（{bundles[0].rsplit('/', 1)[-1]}）")

    if not visible:
        # body 一个字都没有时，一条证据就够判了。
        return evidence
    # 还有零散文字，那至少得两条证据，免得把"服务端渲染的精简页面 + 一个 main.js"错杀。
    return evidence if len(evidence) >= 2 else []


def rendering_hint(tree: Node) -> str:
    """给空结果配一句人话解释；不像单页应用就返回空串。"""
    evidence = detect_client_rendered(tree)
    if not evidence:
        return ""
    return (
        "这个页面疑似由前端 JS 渲染（"
        + "；".join(evidence)
        + "），数据不在 HTML 里。靠解析 HTML 抓不到——需要改用 headless 浏览器，"
        "或者逆向它的 XHR 接口。"
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def extract(
    tree: Node,
    base_url: str,
    selector: str | None = None,
    mode: str = "auto",
    exclude_nodes: Sequence[Node] = (),
) -> DataSet:
    """从页面里抽数据。``mode`` 为 ``auto`` 时按结构自行判断。"""
    if selector:
        scope = select(tree, selector)
        if not scope:
            raise ExtractError(
                f"页面里没有匹配 {selector!r} 的元素。选择器写错了，或者这个页面的结构变了。"
            )
        scope_node = scope[0]
    else:
        body = tree.find("body")
        scope_node = body if body is not None else tree

    notes: list[str] = []

    if mode == "auto":
        candidates = _table_candidates(scope_node, base_url, notes, exclude_nodes)
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            dataset = candidates[0][1]
            dataset.note = "；".join(filter(None, [dataset.note] + notes))
            return dataset

        dataset = _list_dataset(scope_node, base_url, exclude_nodes)
        if dataset is not None:
            return dataset

        dataset = _dl_dataset(scope_node, exclude_nodes)
        if dataset is not None:
            return dataset

        dataset = _cards_dataset(scope_node, base_url, exclude_nodes)
        if dataset is not None:
            return dataset

        dataset = _text_dataset(scope_node, exclude_nodes)
        if not dataset.rows:
            # 一条都没抽到，很可能是页面本身就没内容可抽。说清楚是哪一种，
            # 否则使用者只会反复调 selector——而那个方向根本不通。
            dataset.note = rendering_hint(tree)
        return dataset

    if mode == "table":
        candidates = _table_candidates(scope_node, base_url, notes, exclude_nodes)
        if not candidates:
            raise ExtractError("这个页面上没找到数据表格。试试 --mode auto 或 --mode text。")
        candidates.sort(key=lambda item: item[0], reverse=True)
        dataset = candidates[0][1]
        dataset.note = "；".join(filter(None, [dataset.note] + notes))
        return dataset

    if mode == "list":
        dataset = _list_dataset(scope_node, base_url, exclude_nodes)
        if dataset is None:
            raise ExtractError("这个页面上没找到符合条件的列表（至少 3 个同构条目）。")
        return dataset

    if mode == "cards":
        dataset = _cards_dataset(scope_node, base_url, exclude_nodes)
        if dataset is None:
            raise ExtractError("这个页面上没找到卡片式列表。")
        return dataset

    if mode == "kv":
        dataset = _dl_dataset(scope_node, exclude_nodes)
        if dataset is not None:
            return dataset
        for _, candidate, _ in _table_candidates(scope_node, base_url, notes, exclude_nodes):
            if candidate.kind == KV:
                return candidate
        raise ExtractError("这个页面上没找到键值对结构。")

    if mode == "text":
        return _text_dataset(scope_node, exclude_nodes)

    raise ExtractError(f"不认识的提取模式：{mode!r}")
