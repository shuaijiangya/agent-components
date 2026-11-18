"""识别规范 Markdown 中的 GFM 表格范围。"""

import re

TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")


def markdown_table_ranges(text: str) -> list[tuple[int, int]]:
    """识别 Markdown 表头、分隔行和连续数据行。

    Args:
        text: 已由 Docling 渲染或调用方提供的 Markdown。

    Returns:
        围栏代码之外的 GFM 表格有序区间。
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
            and TABLE_SEPARATOR.fullmatch(lines[index + 1].strip())
        ):
            end = index + 2
            while end < len(lines) and lines[end].strip() and "|" in lines[end]:
                end += 1
            result.append((positions[index], positions[end]))
            index = end
        else:
            index += 1
    return result
