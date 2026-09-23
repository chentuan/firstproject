"""够用的 HTML 树与 CSS 选择器。

标准库给了流式的 ``HTMLParser``，以及零个选择器引擎。要做通用抓取，这两块
都得自己补：先把事件流堆成一棵树，再在树上跑一个选择器子集。

选择器支持 ``tag`` / ``*`` / ``.class`` / ``#id`` / ``[attr]`` / ``[attr=value]``，
以及空格（后代）和 ``>``（直接子元素）两种组合。不支持 ``:nth-child``、``+``、
``~`` 这类兄弟/伪类选择器——抓后台数据用不上，加了只是负担。

不支持 jQuery 那种扩展语法，也不打算支持。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Iterator, Sequence

# 自闭合标签，不压栈。
VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

# 真实页面里大量标签不写闭合（<li>、<td>、<tr> 最典型）。浏览器按这套规则
# 隐式闭合，不模拟它的话树会一路套下去，表格和列表全塌成一层。
# 键是"新开的标签"，值是"遇到它时应当先闭掉的那些处于栈顶的标签"。
IMPLICIT_CLOSE = {
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "p": frozenset({"p"}),
    "option": frozenset({"option"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
    "tr": frozenset({"td", "th", "tr"}),
    "thead": frozenset({"td", "th", "tr", "thead", "tbody", "tfoot"}),
    "tbody": frozenset({"td", "th", "tr", "thead", "tbody", "tfoot"}),
    "tfoot": frozenset({"td", "th", "tr", "thead", "tbody", "tfoot"}),
}

# 这两个标签的整棵子树都不是给人看的正文，节点也不必建。
SKIP_SUBTREE = frozenset({"template", "noscript"})

# 这两个节点要留着——``<script src=...>`` 的属性是有用信息（页面是不是前端打包的，
# 就看它），但里面的代码绝不能混进正文。
DROP_TEXT_SUBTREE = frozenset({"script", "style"})

# 做内容分析时应当无视的标签：整个丢掉的，加上只留空壳的。
NON_CONTENT_TAGS = SKIP_SUBTREE | DROP_TEXT_SUBTREE

_WHITESPACE_RE = re.compile(r"\s+")


class SelectorError(ValueError):
    """选择器写法不认识。配置写错时给个明确消息，别让人对着空结果猜。"""


class Node:
    """轻量节点。文本节点的 ``tag`` 为 ``None``，内容在 ``text`` 里。

    只保留抓数据用得上的东西：标签名、属性、子节点、父节点。没有命名空间、
    没有样式计算、没有事件——那些不是这个工具该管的事。
    """

    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(
        self,
        tag: str | None,
        attrs: dict[str, str] | None = None,
        parent: "Node | None" = None,
        text: str = "",
    ) -> None:
        self.tag = tag
        self.attrs = attrs if attrs is not None else {}
        self.children: list[Node] = []
        self.parent = parent
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        if self.tag is None:
            preview = self.text[:20].replace("\n", " ")
            return f"<text {preview!r}>"
        return f"<{self.tag} {len(self.children)} children>"

    # ------------------------------------------------------------------ 属性

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name, default)

    @property
    def classes(self) -> tuple[str, ...]:
        return tuple(self.attrs.get("class", "").split())

    @property
    def href(self) -> str | None:
        return self.attrs.get("href")

    # ------------------------------------------------------------------ 遍历

    def walk(self) -> Iterator["Node"]:
        """深度优先遍历所有后代（不含自身）。"""
        for child in self.children:
            yield child
            yield from child.walk()

    def self_and_walk(self) -> Iterator["Node"]:
        """包含自身的遍历。

        ``walk()`` 不含自身是有意的，但抓数据时经常需要"把这个节点也算上"
        ——比如选择器直接指向数据容器本身的时候。
        """
        yield self
        yield from self.walk()

    def elements(self, *tags: str) -> Iterator["Node"]:
        """遍历所有元素节点，给 ``tags`` 就只留这几种标签。"""
        for node in self.walk():
            if node.tag is not None and (not tags or node.tag in tags):
                yield node

    def select(self, selector: str) -> list["Node"]:
        return select(self, selector)

    def find(self, selector: str) -> "Node | None":
        found = select(self, selector)
        return found[0] if found else None

    def ancestors(self) -> Iterator["Node"]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def siblings(self) -> list["Node"]:
        if self.parent is None:
            return [self]
        return [child for child in self.parent.children if child is not self]

    # ------------------------------------------------------------------ 文本

    def raw_text(self, skip: Sequence[str] = ()) -> str:
        """原样拼接后代文本。``<br>`` 当作换行，跳过的标签整棵不取。"""
        skip_set = set(skip) | NON_CONTENT_TAGS
        chunks: list[str] = []
        for child in self.children:
            if child.tag is None:
                chunks.append(child.text)
            elif child.tag in skip_set:
                continue
            elif child.tag == "br":
                chunks.append("\n")
            else:
                chunks.append(child.raw_text(skip))
        return "".join(chunks)

    def clean_text(self, skip: Sequence[str] = ()) -> str:
        """折叠空白后的文本。单元格取值一律用这个。"""
        text = self.raw_text(skip).replace("\xa0", " ")
        return _WHITESPACE_RE.sub(" ", text).strip()


# --------------------------------------------------------------------------- #
# 建树
# --------------------------------------------------------------------------- #

class _TreeBuilder(HTMLParser):
    """把 HTMLParser 的事件流堆成一棵宽容的树。

    宽容的意思：闭合标签对不上就忽略，不抛异常。真实后台的 HTML 是模板引擎
    吐出来的，多一个少一个 ``</div>`` 很常见，为此中断整个抓取不值得。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self._stack: list[Node] = [self.root]
        self._skip_depth = 0

    def _top(self) -> Node:
        return self._stack[-1]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()

        if self._skip_depth:
            if tag in SKIP_SUBTREE:
                self._skip_depth += 1
            return
        if tag in SKIP_SUBTREE:
            self._skip_depth = 1
            return

        closable = IMPLICIT_CLOSE.get(tag)
        if closable:
            while len(self._stack) > 1 and self._stack[-1].tag in closable:
                self._stack.pop()

        # 属性名统一小写：HTMLParser 只保证标签名小写，属性名原样保留，
        # 而模板里写成 `HREF=` / `Action=` 的情况真的存在。
        attributes: dict[str, str] = {}
        for key, value in attrs:
            key = key.lower()
            if key not in attributes:
                attributes[key] = value if value is not None else ""

        node = Node(tag, attributes, self._top())
        self._top().children.append(node)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if tag in SKIP_SUBTREE:
                self._skip_depth -= 1
            return
        if tag in VOID_TAGS:
            return
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return
        # 栈里找不到对应的开标签，无视它。

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        # script / style 的内容是代码不是正文：节点留着（属性还有用），文本丢掉。
        if any(node.tag in DROP_TEXT_SUBTREE for node in self._stack):
            return
        self._top().children.append(Node(None, None, self._top(), data))


def parse_html(html: str) -> Node:
    """HTML 字符串 → 根节点。"""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


# --------------------------------------------------------------------------- #
# 选择器
# --------------------------------------------------------------------------- #

class _Compound:
    """一个复合选择器片段，比如 ``aside.sidebar`` 或 ``[role=navigation]``。"""

    __slots__ = ("tag", "id", "classes", "attrs")

    def __init__(
        self,
        tag: str | None,
        id: str | None,
        classes: tuple[str, ...],
        attrs: tuple[tuple[str, str | None], ...],
    ) -> None:
        self.tag = tag
        self.id = id
        self.classes = classes
        self.attrs = attrs


_COMPOUND_RE = re.compile(
    r"(?P<tag>[A-Za-z][\w-]*|\*)?(?P<rest>(?:\.[\w-]+|\#[\w-]+|\[[^\]]*\])*)"
)
_SIMPLE_RE = re.compile(r"\.([\w-]+)|#([\w-]+)|\[([^\]]*)\]")


def _split_selector(selector: str) -> list[tuple[str, str]]:
    """把 ``"aside.sidebar > a"`` 拆成 ``[(" ", "aside.sidebar"), (">", "a")]``。

    手写扫描而不是正则切分，因为 ``[href="a b"]`` 里的空格不是组合符。
    """
    tokens: list[tuple[str, str]] = []
    buffer = ""
    combinator = " "

    def flush() -> None:
        nonlocal buffer, combinator
        if buffer:
            tokens.append((combinator, buffer))
            buffer = ""
            combinator = " "

    index = 0
    while index < len(selector):
        char = selector[index]
        if char.isspace():
            flush()
            index += 1
            continue
        if char == ">":
            flush()
            combinator = ">"
            index += 1
            continue
        if char == "[":
            end = selector.find("]", index)
            if end == -1:
                raise SelectorError(f"选择器里的方括号没闭合：{selector!r}")
            buffer += selector[index : end + 1]
            index = end + 1
            continue
        buffer += char
        index += 1

    flush()
    if not tokens:
        raise SelectorError(f"选择器是空的：{selector!r}")
    return tokens


def _parse_compound(text: str) -> _Compound:
    match = _COMPOUND_RE.fullmatch(text)
    if match is None:
        raise SelectorError(f"看不懂的选择器片段：{text!r}")

    tag = match.group("tag")
    if tag == "*":
        tag = None

    id_: str | None = None
    classes: list[str] = []
    attrs: list[tuple[str, str | None]] = []

    for dot, hash_, bracket in _SIMPLE_RE.findall(match.group("rest") or ""):
        if dot:
            classes.append(dot)
        elif hash_:
            id_ = hash_
        else:
            if "=" in bracket:
                key, _, value = bracket.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                attrs.append((key.strip(), value))
            else:
                attrs.append((bracket.strip(), None))

    return _Compound(tag, id_, tuple(classes), tuple(attrs))


def _matches(node: Node, compound: _Compound) -> bool:
    if node.tag is None:
        return False
    if compound.tag is not None and node.tag != compound.tag:
        return False
    if compound.id is not None and node.get("id") != compound.id:
        return False
    for name in compound.classes:
        if name not in node.classes:
            return False
    for key, value in compound.attrs:
        actual = node.get(key)
        if actual is None:
            return False
        if value is not None and actual != value:
            return False
    return True


def select(root: Node, selector: str) -> list[Node]:
    """在 ``root`` 的后代里跑选择器，返回文档顺序的匹配列表。"""
    chain = [(_parse_compound(text), combinator) for combinator, text in _split_selector(selector)]

    current = [node for node in root.walk() if _matches(node, chain[0][0])]

    for compound, combinator in chain[1:]:
        matched: list[Node] = []
        if combinator == ">":
            for node in current:
                for child in node.children:
                    if _matches(child, compound):
                        matched.append(child)
        else:
            for node in current:
                for descendant in node.walk():
                    if _matches(descendant, compound):
                        matched.append(descendant)
        current = matched

    return current


def select_first(root: Node, selector: str) -> Node | None:
    found = select(root, selector)
    return found[0] if found else None
