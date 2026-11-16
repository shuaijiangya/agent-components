"""按 GFM 表格行保留结构并重复表头。

正文始终是原文连续切片；后续片的重复表头单独记录，不伪造正文字符偏移。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from agent_components.core.contracts import TableSlice

_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")


@dataclass(frozen=True)
class SpanPiece:
    """在分块执行阶段保存区间与额外上下文。

    prefix 的来源记录在 table.header_start/header_end 中。
    """

    start: int
    end: int
    prefix: str = ""
    table: TableSlice | None = None


def markdown_table_ranges(text: str) -> list[tuple[int, int]]:
    """识别 Markdown 表头、分隔行和连续数据行。

    Args:
        text: 已由 Docling 渲染或调用方提供的 Markdown。

    Returns:
        完整 GFM 表格的有序区间，不把普通标题文本当成结构标题。
    """
    lines = text.splitlines(keepends=True)
    positions = [0]
    for line in lines:
        positions.append(positions[-1] + len(line))
    result = []
    index = 0
    fence: tuple[str, int] | None = None
    while index < len(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", lines[index].rstrip("\r\n"))
        if marker:
            symbol, tail = marker.groups()
            if fence is None:
                fence = (symbol[0], len(symbol))
            elif symbol[0] == fence[0] and len(symbol) >= fence[1] and not tail.strip():
                fence = None
            index += 1
            continue
        if (
            fence is None
            and index + 1 < len(lines)
            and "|" in lines[index]
            and _SEPARATOR.fullmatch(lines[index + 1].strip())
        ):
            end = index + 2
            while end < len(lines) and lines[end].strip() and "|" in lines[end]:
                end += 1
            result.append((positions[index], positions[end]))
            index = end
        else:
            index += 1
    return result


def split_table(
    text: str,
    start: int,
    end: int,
    table_id: str,
    *,
    size: int,
    measure: Callable[[str], int],
    overflow: str,
) -> list[SpanPiece]:
    """按完整数据行切超长表格，为后续分片重复表头。

    Args:
        text: 完整规范文本。
        start: 表格包含端点。
        end: 表格排除端点。
        table_id: 原生元素身份或 Markdown 表格身份。
        size: 包含重复表头的预算。
        measure: 字符或实际 Token 计数器。
        overflow: error 拒绝无法容纳的一行，allow 显式允许并标记 oversized。

    Returns:
        不切断数据行的片段，带数据行范围和表头来源。

    Raises:
        ValueError: 表头加单行超过预算且 overflow 为 error。
    """
    lines = text[start:end].splitlines(keepends=True)
    header_lines = 2 if len(lines) >= 2 and _SEPARATOR.fullmatch(lines[1].strip()) else 0
    if not header_lines:
        if measure(text[start:end]) > size and overflow == "error":
            raise ValueError("non-GFM table exceeds size; use table_overflow='allow'")
        return [
            SpanPiece(
                start,
                end,
                table=TableSlice(
                    table_id=table_id,
                    row_start=0,
                    row_end=0,
                    header_start=start,
                    header_end=start,
                    oversized=measure(text[start:end]) > size,
                ),
            )
        ]
    header_end = start + sum(map(len, lines[:header_lines]))
    header = text[start:header_end]
    # 原文末尾表头无换行时，也要保证重复前缀与数据行分隔；不改变原文正文。
    prefix = header if header.endswith(("\n", "\r")) else header + "\n"
    positions = [header_end]
    for line in lines[header_lines:]:
        positions.append(positions[-1] + len(line))
    pieces: list[SpanPiece] = []
    row = 0
    while row < len(positions) - 1 or (row == 0 and not pieces):
        left = start if not pieces else positions[row]
        extra = "" if not pieces else prefix
        stop = row
        while (
            stop < len(positions) - 1
            and (
                len(extra) + positions[stop + 1] - left
                if measure is len
                else measure(extra + text[left : positions[stop + 1]])
            )
            <= size
        ):
            stop += 1
        if stop == row and row < len(positions) - 1:
            stop += 1
        right = positions[stop]
        oversized = measure(extra + text[left:right]) > size
        if oversized and overflow == "error":
            raise ValueError("table header plus one row exceeds size; use table_overflow='allow'")
        pieces.append(
            SpanPiece(
                left,
                right,
                extra,
                TableSlice(
                    table_id=table_id,
                    row_start=row,
                    row_end=stop,
                    header_start=start,
                    header_end=header_end,
                    oversized=oversized,
                ),
            )
        )
        if stop == len(positions) - 1:
            break
        row = stop
    return pieces
