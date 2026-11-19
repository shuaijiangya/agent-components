"""从 Docling 原生标题层级生成不重叠的章节范围。

不通过 Markdown 的井号或文本相似度推测标题，保留完整层级路径。
"""

from bisect import bisect_right
from collections.abc import Iterator, Sequence

from pydantic import Field

from agent_components.chunking.tables import SpanPiece, split_table
from agent_components.core.contracts import Contract, Document

DEFAULT_MAX_PARENT_CHARS = 4000
_SECTION_LAYOUT_VERSION = "bounded-sections-v1"


class SectionOptions(Contract):
    """配置章节识别和自动父块的字符预算。

    max_parent_chars 包含表格的重复表头，与子块字符或 Token 预算独立。
    """

    max_level: int = Field(default=6, ge=1, strict=True)
    include_preamble: bool = True
    max_parent_chars: int = Field(default=DEFAULT_MAX_PARENT_CHARS, ge=1, strict=True)


class Section(Contract):
    """描述一个分节的原文范围和标题路径。

    level 为零时表示没有标题的全文或序言。
    """

    start: int
    end: int
    level: int
    path: tuple[str, ...]


class SectionBuilder:
    """把指定标题深度以内的边界平铺为章节父范围。

    父子块采用两层结构；标题祖先关系通过 path 保留，不生成相互重叠的父范围。
    """

    def build(
        self,
        document: Document,
        *,
        max_level: int = 6,
        include_preamble: bool = True,
    ) -> tuple[Section, ...]:
        """根据结构标题生成连续分节。

        Args:
            document: Docling 解析产物或显式提供结构的规范文档。
            max_level: 作为边界的最大标题层级，必须为正整数。
            include_preamble: 是否保留第一个标题之前的文本。

        Returns:
            按原文顺序的章节；没有标题时将全文视为一节，空文本返回空元组。

        Raises:
            ValueError: max_level 非正整数。
        """
        if type(max_level) is not int or max_level < 1:
            raise ValueError("max_level must be a positive integer")
        if not document.text:
            return ()
        headings = [
            e
            for e in document.elements
            if e.kind == "heading" and e.level is not None and e.level <= max_level
        ]
        if not headings:
            return (Section(start=0, end=len(document.text), level=0, path=()),)
        sections = []
        if include_preamble and headings[0].start:
            sections.append(Section(start=0, end=headings[0].start, level=0, path=()))
        stack: list[tuple[int, str]] = []
        for index, heading in enumerate(headings):
            assert heading.level is not None
            while stack and stack[-1][0] >= heading.level:
                stack.pop()
            stack.append((heading.level, heading.title or ""))
            end = headings[index + 1].start if index + 1 < len(headings) else len(document.text)
            sections.append(
                Section(
                    start=heading.start,
                    end=end,
                    level=heading.level,
                    path=tuple(title for _, title in stack),
                )
            )
        return tuple(sections)


def _body_pieces(text: str, start: int, end: int, limit: int) -> Iterator[SpanPiece]:
    """在窗口后半段优先使用段落、行或句末边界，否则按字符上限切开。"""
    while start < end:
        stop = min(start + limit, end)
        if stop < end:
            floor = start + limit // 2
            for delimiters in (("\n\n",), ("\n",), ("。", "！", "？", ". ", "! ", "? ")):
                candidates = [(text.rfind(d, floor, stop), len(d)) for d in delimiters]
                boundary = max((pos + size for pos, size in candidates if pos >= 0), default=0)
                if boundary:
                    stop = boundary
                    break
        yield SpanPiece(start, stop)
        start = stop


def _bounded_parents(
    document: Document,
    sections: Sequence[Section],
    tables: Sequence[tuple[int, int, str]],
    limit: int,
) -> tuple[tuple[SpanPiece, tuple[str, ...]], ...]:
    """为章节父块生成有界范围，长表格按完整行分片。

    章节路径沿用原标题。表格父片的正文取原文，后续片另携带重复表头。
    若一行加表头仍超过父预算，抛出错误，避免悄悄产生超预算的父块。
    """
    result = []
    table_ends = tuple(end for _, end, _ in tables)
    for section in sections:
        section_tables = []
        for index in range(bisect_right(table_ends, section.start), len(tables)):
            start, end, table_id = tables[index]
            if start >= section.end:
                break
            if not section.start <= start < end <= section.end:
                raise ValueError("section boundary cuts through a preserved table")
            section_tables.append((start, end, table_id))
        if section.end - section.start <= limit:
            result.append((SpanPiece(section.start, section.end), section.path))
            continue
        position = section.start
        for start, end, table_id in section_tables:
            result.extend(
                (p, section.path) for p in _body_pieces(document.text, position, start, limit)
            )
            try:
                parts = split_table(
                    document.text, start, end, table_id, size=limit, measure=len, overflow="error"
                )
            except ValueError as exc:
                raise ValueError(
                    "preserved table row or indivisible table exceeds max_parent_chars"
                ) from exc
            result.extend((p, section.path) for p in parts)
            position = end
        result.extend(
            (p, section.path) for p in _body_pieces(document.text, position, section.end, limit)
        )
    return tuple(result)
