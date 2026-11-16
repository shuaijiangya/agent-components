import pytest

pytest.importorskip("docling_core")
from docling_core.types.doc import DocItemLabel, DoclingDocument

from agent_components.chunking import Chunker, ChunkPolicy, ParentChildBuilder
from agent_components.parsing import DoclingParser


def test_native_docling_repeated_text_heading_levels_and_page_provenance():
    from docling_core.types.doc import BoundingBox, CoordOrigin, ProvenanceItem

    native = DoclingDocument(name="source-map")
    native.add_heading(text="合同", level=1)
    for page in (1, 2):
        native.add_text(
            label=DocItemLabel.PARAGRAPH,
            text="同样的付款条款。",
            prov=ProvenanceItem(
                page_no=page,
                charspan=(0, 8),
                bbox=BoundingBox(l=10, t=20, r=100, b=40, coord_origin=CoordOrigin.TOPLEFT),
            ),
        )
        native.add_heading(text=f"第{page}节", level=2)
    parsed = DoclingParser().from_native(native, document_id="mapped", revision_id="r1")
    assert parsed.source_mapping == "mapped"
    refs = [span for span in parsed.document.source_map if any(r.page for r in span.refs)]
    assert len(refs) == 2 and refs[0].start != refs[1].start
    result = Chunker().chunk(parsed.document, ChunkPolicy(strategy="character", size=4))
    first = [c for c in result.leaves if refs[0].start <= c.start < refs[0].end]
    second = [c for c in result.leaves if refs[1].start <= c.start < refs[1].end]
    assert any(r.page == 1 for c in first for r in c.source_refs)
    assert any(r.page == 2 for c in second for r in c.source_refs)
    assert any(r.bbox == (10, 20, 100, 40) for c in second for r in c.source_refs)
    sections = ParentChildBuilder().build_sections(
        parsed.document, ChunkPolicy(strategy="character", size=5)
    )
    assert len(sections.parents) == 3
    assert any(c.section_path == ("合同", "第1节") for c in sections.parents)


@pytest.mark.integration
@pytest.mark.parametrize(
    "extension,content",
    [
        (
            ".md",
            (
                "# 合同\n\n付款条款。\n\n## 明细\n\n| 项目 | 金额 |\n| --- | --- |\n"
                "| 甲 | 000123 |\n| 乙 | 3200.00 |"
            ),
        ),
        (
            ".html",
            (
                "<h1>合同</h1><p>付款条款。</p><h2>明细</h2><table>"
                "<tr><th>项目</th><th>金额</th></tr><tr><td>甲</td><td>000123</td></tr>"
                "<tr><td>乙</td><td>3200.00</td></tr></table>"
            ),
        ),
    ],
)
def test_real_docling_files_produce_sections_mapped_tables_and_preserve_numeric_text(
    tmp_path, extension, content
):
    pytest.importorskip("docling")
    path = tmp_path / ("contract" + extension)
    path.write_text(content, encoding="utf-8")
    parsed = DoclingParser().parse(path, document_id="contract")
    assert parsed.source_mapping == "mapped"
    assert any(e.kind == "table" for e in parsed.document.elements)
    assert any(e.kind == "heading" for e in parsed.document.elements)
    assert "000123" in parsed.document.text and "3200.00" in parsed.document.text
    result = ParentChildBuilder().build_sections(
        parsed.document, ChunkPolicy(strategy="character", size=90, table_overflow="allow")
    )
    assert any(c.table for c in result.leaves)
    assert all(c.text == parsed.document.text[c.start : c.end] for c in result.leaves)
    assert all(any(r.element_id for r in c.source_refs) for c in result.leaves if c.table)


@pytest.mark.integration
def test_real_docling_docx_structures_and_mapped_table(tmp_path):
    import zipfile

    pytest.importorskip("docling")
    path = tmp_path / "contract.docx"
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "docx"
    files = {
        p.relative_to(fixture).as_posix(): p.read_text() for p in fixture.rglob("*") if p.is_file()
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    result = DoclingParser().parse(path)
    assert result.source_mapping == "mapped"
    assert "000123" in result.document.text
    assert any(e.kind == "heading" for e in result.document.elements)
    assert any(e.kind == "table" for e in result.document.elements)


@pytest.mark.integration
def test_pdf_batch_ranges_use_real_pdf_page_count_and_keep_distinct_segment_ids(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    pdfium = pytest.importorskip("pypdfium2")
    path = tmp_path / "pages.pdf"
    native_pdf = pdfium.PdfDocument.new()
    for _ in range(5):
        page = native_pdf.new_page(100, 100)
        page.close()
    native_pdf.save(str(path))
    native_pdf.close()
    calls = []

    class Converter:
        def convert(self, source, *, page_range, raises_on_error):
            calls.append(page_range)
            native = DoclingDocument(name="page-batch")
            native.add_heading(text=f"页 {page_range}", level=1)
            return SimpleNamespace(document=native)

    parser = DoclingParser()
    monkeypatch.setattr(parser, "_converter", lambda path: Converter())
    results = list(parser.iter_pdf(path, document_id="original", pages_per_batch=2))
    assert calls == [(1, 2), (3, 4), (5, 5)]
    assert len({r.document.id for r in results}) == 3
    assert all(r.document.metadata["source_document_id"] == "original" for r in results)
    assert results[-1].document.metadata["page_start"] == 5
    assert len({r.document.revision_id for r in results}) == 1
