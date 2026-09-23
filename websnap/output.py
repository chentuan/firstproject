"""输出层：表格 / CSV / JSON。

表格对齐按显示宽度算，中日韩全角字符占 2 列——直接 ``len()`` 的中文列在终端
里会歪，这个坑没必要每个人都踩一遍。

CSV 写文件时加 UTF-8 BOM，否则 Excel 会把中文表头读成乱码。这是 Excel 的问题，
但挨骂的是我们。
"""

from __future__ import annotations

import csv
import io
import json
import unicodedata
from datetime import datetime
from typing import Any, Sequence

from .extract import DataSet

# 这些字段是元数据，不作为列展示。
INTERNAL_FIELDS = ("_links",)


def display_width(text: str) -> int:
    """终端显示宽度。全角/宽字符算 2 列。"""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def visible_columns(dataset: DataSet) -> list[str]:
    return [column for column in dataset.columns if column not in INTERNAL_FIELDS]


def safe_filename(text: str) -> str:
    """把菜单名变成能当文件名的字符串。

    菜单名里出现 ``/`` ``:`` 这类字符是常态（"订单/售后"），直接拿去建文件
    会得到随机目录或直接报错。CLI 和 GUI 共用这一份实现。
    """
    cleaned = "".join("_" if char in '/\\:*?"<>|' else char for char in text).strip()
    return cleaned or "data"


def render_table(dataset: DataSet) -> str:
    """对齐的纯文本表格。没有行时返回表头加一行说明。"""
    columns = visible_columns(dataset)
    if not columns:
        return "(没有可展示的列)"

    rows = [
        {column: str(row.get(column, "")) for column in columns} for row in dataset.rows
    ]

    widths = []
    for column in columns:
        width = display_width(column)
        for row in rows:
            width = max(width, display_width(row[column]))
        widths.append(width)

    lines = [
        "  ".join(pad_display(column, width) for column, width in zip(columns, widths)),
        "  ".join("-" * width for width in widths),
    ]
    for row in rows:
        lines.append(
            "  ".join(pad_display(row[column], width) for column, width in zip(columns, widths))
        )
    if not rows:
        lines.append("(0 条数据)")
    return "\n".join(lines)


def render_csv(dataset: DataSet, with_bom: bool = True) -> str:
    columns = visible_columns(dataset)
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    for row in dataset.rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    text = buffer.getvalue()
    return ("\ufeff" + text) if with_bom else text


def build_payload(
    dataset: DataSet,
    site: str | None = None,
    menu_name: str | None = None,
    menu_path: str | None = None,
    fetched_at: str | None = None,
    page_url: str | None = None,
) -> dict[str, Any]:
    """JSON 输出的结构。带上足够的上下文，拿到文件的人不用回头问你。"""
    payload: dict[str, Any] = {
        "site": site,
        "menu": {"name": menu_name, "path": menu_path, "url": page_url},
        "fetched_at": fetched_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": dataset.kind,
        "columns": visible_columns(dataset),
        "count": dataset.count,
        "rows": dataset.rows,
    }
    if dataset.note:
        payload["note"] = dataset.note
    return payload


def render_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_heading(
    menu_name: str | None,
    menu_path: str | None,
    dataset: DataSet,
    fetched_at: str | None = None,
) -> str:
    """终端里每个菜单前的一行小标题。"""
    title = menu_name or menu_path or "(未知菜单)"
    bits = [title]
    if menu_path:
        bits.append(menu_path)
    bits.append(dataset.describe())
    if fetched_at:
        bits.append(fetched_at)
    return " · ".join(bits)


def print_datasets(
    results: Sequence[tuple[str | None, str | None, DataSet]],
) -> None:
    """终端打印多个菜单的结果，中间空一行。"""
    for index, (name, path, dataset) in enumerate(results):
        if index:
            print()
        print(render_heading(name, path, dataset))
        print(render_table(dataset))
        if dataset.note:
            print(f"提示：{dataset.note}")
