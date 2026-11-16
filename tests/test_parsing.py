import pytest

from agent_components.parsing import DoclingParser


def test_plain_text_requires_no_docling_models(tmp_path):
    path = tmp_path / "contract.txt"
    path.write_text("供方：甲公司\n金额：3200元", encoding="utf-8-sig")
    parsed = DoclingParser().parse(path)
    assert parsed.document.text.startswith("供方：甲公司")
    assert parsed.source_mapping == "text_only"


def test_pdf_never_downloads_models_implicitly(tmp_path):
    path = tmp_path / "contract.pdf"
    path.write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError, match="artifacts_path"):
        DoclingParser().parse(path)


@pytest.mark.integration
def test_real_docling_markdown_conversion(tmp_path):
    pytest.importorskip("docling")
    path = tmp_path / "terms.md"
    path.write_text("# 服务条款\n\n供方：甲公司。\n\n金额：3200元。", encoding="utf-8")
    parsed = DoclingParser().parse(path, document_id="terms")
    assert parsed.document.id == "terms"
    assert "3200" in parsed.document.text
    assert parsed.native_json
    assert parsed.source_mapping == "mapped"
