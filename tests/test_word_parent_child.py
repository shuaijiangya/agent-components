from pathlib import Path

import pytest

from agent_components.chunking import ChunkPolicy, ParentChildBuilder
from agent_components.parsing import DoclingParser


@pytest.mark.integration
def test_word_parent_child_chunking():
    pytest.importorskip("docling")
    word_path = Path(__file__).parent / "fixtures" / "parent_child_sample.docx"
    parsed = DoclingParser().parse(word_path, document_id="word-chunk-demo")
    document = parsed.document
    headings = [(item.level, item.title) for item in document.elements if item.kind == "heading"]

    # 预留 Markdown 表格在有无 wcwidth 时的列填充差异。
    policy = ChunkPolicy(strategy="character", size=175)
    builder = ParentChildBuilder()
    by_chapter = builder.build_sections(document, policy, max_level=1)
    by_section = builder.build_sections(document, policy, max_level=2)

    assert headings == [
        (1, "采购合同审批与验收规范"),
        (1, "第一章 采购合同审批"),
        (2, "第一节 提交审批"),
        (2, "第二节 复核与归档"),
        (1, "第二章 交付与验收"),
        (2, "第一节 到货核验"),
        (2, "第二节 材料归档"),
    ]
    assert (len(by_chapter.parents), len(by_chapter.leaves)) == (3, 7)
    assert (len(by_section.parents), len(by_section.leaves)) == (7, 9)

    for result in (by_chapter, by_section):
        parents_by_id = {parent.id: parent for parent in result.parents}
        for leaf in result.leaves:
            parent = parents_by_id[leaf.parent_id]
            assert parent.start <= leaf.start < leaf.end <= parent.end
            assert leaf.text == document.text[leaf.start : leaf.end]
            assert leaf.section_path == parent.section_path

    first_table_chunk, second_table_chunk = [leaf for leaf in by_section.leaves if leaf.table]
    assert first_table_chunk.table is not None
    assert second_table_chunk.table is not None
    assert (first_table_chunk.table.row_start, first_table_chunk.table.row_end) == (0, 2)
    assert (second_table_chunk.table.row_start, second_table_chunk.table.row_end) == (2, 3)
    header_start = second_table_chunk.table.header_start
    header_end = second_table_chunk.table.header_end
    assert first_table_chunk.context_prefix == ""
    assert second_table_chunk.context_prefix == document.text[header_start:header_end]
