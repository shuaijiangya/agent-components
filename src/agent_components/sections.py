"""从 Docling 原生标题层级生成不重叠的章节范围。

不通过 Markdown 的井号或文本相似度推测标题，保留完整层级路径。
"""

from agent_components.core.contracts import Contract, Document


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
