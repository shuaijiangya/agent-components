import pytest

from agent_components.chunking import ChunkPolicy, ParentChildBuilder
from agent_components.core.contracts import Document, DocumentElement, ParsedDocument
from agent_components.knowledge_base import KnowledgeBase
from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository
from agent_components.parsing import DoclingParser
from agent_components.retrieval import ContextExpander


@pytest.mark.parametrize("has_heading", [False, True])
def test_bounded_section_parents_fit_retrieval_budget(has_heading):
    text = "付款条款。" * 2000
    elements = (
        (DocumentElement(id="h", kind="heading", start=0, end=5, level=1, title="付款条款"),)
        if has_heading
        else ()
    )
    document = Document(id="contract", text=text, elements=elements)
    builder = ParentChildBuilder()
    result = builder.build_sections(document, ChunkPolicy(strategy="character", size=512))
    parents = {parent.id: parent for parent in result.parents}
    assert len(parents) > 1
    assert "".join(p.text for p in result.parents) == text
    assert all(len(p.index_text) <= 4000 for p in result.parents)
    assert {p.section_path for p in parents.values()} == ({("付款条款",)} if has_heading else {()})
    assert all(
        parents[leaf.parent_id].start <= leaf.start < leaf.end <= parents[leaf.parent_id].end
        for leaf in result.leaves
    )
    context = ContextExpander().expand(result.leaves[:1], result.chunks, parent_mode="replace")
    assert context.windows and not context.omitted_ids


def test_long_table_spans_parent_ranges_without_changing_row_or_header_offsets():
    intro = "# 费用\n\n"
    header = "| 日期 | 金额 |\n| --- | --- |\n"
    rows = [f"| 第{i:02}天 | {i * 1000}元 |\n" for i in range(1, 18)]
    text = intro + header + "".join(rows) + "\n补充条款。"
    document = Document(
        id="table",
        text=text,
        elements=(
            DocumentElement(id="h", kind="heading", start=0, end=4, level=1, title="费用"),
            DocumentElement(
                id="fees", kind="table", start=len(intro), end=len(intro + header + "".join(rows))
            ),
        ),
    )
    policy = ChunkPolicy(strategy="character", size=55)
    result = ParentChildBuilder().build_sections(document, policy, max_parent_chars=105)
    assert "".join(parent.text for parent in result.parents) == text
    assert all(len(parent.index_text) <= 105 for parent in result.parents)
    fees = [leaf for leaf in result.leaves if leaf.table is not None]
    assert len({leaf.parent_id for leaf in fees}) > 1
    assert [leaf.table.row_start for leaf in fees] == list(range(len(rows)))
    assert [leaf.table.row_end for leaf in fees] == list(range(1, len(rows) + 1))
    assert [leaf.index_text for leaf in fees] == [header + row for row in rows]
    assert all(
        leaf.table.table_id == "fees"
        and leaf.table.header_start == len(intro)
        and leaf.table.header_end == len(intro + header)
        for leaf in fees
    )
    assert all(leaf.text == text[leaf.start : leaf.end] for leaf in result.leaves)
    with pytest.raises(ValueError, match="max_parent_chars"):
        ParentChildBuilder().build_sections(document, policy, max_parent_chars=12)


def test_rechunk_versions_can_coexist_and_retain_parser_artifact_trace(tmp_path):
    from agent_components.chunking import Rechunker, SectionOptions

    file = tmp_path / "original.txt"
    payload = "验收通过后支付尾款。" * 20
    file.write_text(payload, encoding="utf-8")
    parsed = DoclingParser().parse(file, document_id="contract")
    artifact = tmp_path / "original.parsed.json"
    artifact.write_text(parsed.model_dump_json(), encoding="utf-8")
    file.unlink()
    cached = ParsedDocument.model_validate_json(artifact.read_bytes())
    strategy = ChunkPolicy(strategy="character", size=12)
    rechunker = Rechunker()
    initial = rechunker.rechunk(cached, strategy, artifact_ref=artifact.name)
    strategy = ChunkPolicy(strategy="character", size=8)
    changed = rechunker.rechunk(
        cached,
        strategy,
        section_options=SectionOptions(max_parent_chars=40),
        artifact_ref=artifact.name,
        previous_revision_id=initial.document.revision_id,
    )
    assert (
        changed.document.revision_id != initial.document.revision_id != cached.document.revision_id
    )
    same = rechunker.rechunk(
        cached,
        strategy,
        section_options=SectionOptions(max_parent_chars=40),
        artifact_ref=artifact.name,
        previous_revision_id=initial.document.revision_id,
    )
    assert same == changed
    assert all(leaf.revision_id == changed.document.revision_id for leaf in changed.chunks.leaves)
    assert not {leaf.id for leaf in initial.chunks.leaves} & {
        leaf.id for leaf in changed.chunks.leaves
    }
    assert changed.trace.source_revision_id == cached.document.revision_id
    assert changed.trace.previous_revision_id == initial.document.revision_id
    assert changed.trace.parsing.backend == "plain_text"
    assert changed.trace.artifact_ref == artifact.name
    assert changed.trace.artifact_sha256 == initial.trace.artifact_sha256
    assert changed.trace.matches_artifact(cached)
    assert changed.trace.chunk_config_fingerprint == changed.chunks.config_fingerprint
    assert changed.trace.chunk_policy == strategy
    assert changed.trace.section_options.max_parent_chars == 40
    assert {len(parent.text) for parent in changed.chunks.parents} <= set(range(1, 41))
    store = KnowledgeBase("contracts", SQLiteKnowledgeRepository(tmp_path / "knowledge.db"))
    store.save_revision(initial.document, initial.chunks)
    store.publish_revision("contract", initial.document.revision_id, expected_revision=None)
    store.save_revision(changed.document, changed.chunks)
    store.save_revision(same.document, same.chunks)
    stored = store.get_revision("contract", changed.document.revision_id)
    assert stored is not None
    assert stored.document.metadata["agent_components.rechunk"] == changed.trace.model_dump(
        mode="json"
    )
    assert store.published_chunks(document_ids=["contract"]) == initial.chunks.leaves


def test_legacy_parsed_artifact_supports_rechunk_without_assuming_parser_options():
    from agent_components.chunking import Rechunker

    source = ParsedDocument.model_validate(
        {
            "document": {"id": "d", "text": "材料清单。"},
            "markdown": "材料清单。",
            "native_json": {"adapter": "legacy"},
        }
    )
    policy = ChunkPolicy(strategy="character", size=5)
    one = Rechunker().rechunk(source, policy)
    corrected = source.model_copy(
        update={"native_json": {"adapter": "legacy", "basis": "corrected"}}
    )
    two = Rechunker().rechunk(corrected, policy)
    assert one.trace.parsing is None
    assert two.trace.artifact_sha256 != one.trace.artifact_sha256
    assert not one.trace.matches_artifact(corrected)
    assert two.document.revision_id != one.document.revision_id


def test_rechunk_cli_honors_parent_budget_without_original_file(tmp_path):
    from test_examples import successful, write_json

    source = tmp_path / "terms.txt"
    source.write_text("先确认费用再付款。" * 12, encoding="utf-8")
    parsed = write_json(tmp_path / "parsed.json", successful("documents", "parse", source))
    source.unlink()
    policy = write_json(tmp_path / "policy.json", {"strategy": "character", "size": 12})
    revision = successful(
        "documents",
        "rechunk",
        parsed,
        "--policy",
        policy,
        "--by-sections",
        "--max-parent-chars",
        40,
        "--artifact-ref",
        "parsed.json",
    )
    parents = [chunk for chunk in revision["chunks"]["chunks"] if chunk["kind"] == "parent"]
    assert len(parents) > 1 and all(len(chunk["text"]) <= 40 for chunk in parents)
    assert revision["trace"]["artifact_ref"] == "parsed.json"
    assert revision["document"]["revision_id"].startswith("rechunk-")


def test_explicit_parents_keep_markdown_table_outside_native_table():
    table = "| 材料 |\n| --- |\n| 合同 |\n| 清单 |\n"
    text = f"{table}\n{table}"
    markdown_start = len(table) + 1
    document = Document(
        id="mixed",
        text=text,
        elements=(DocumentElement(id="native", kind="table", start=0, end=len(table)),),
    )
    result = ParentChildBuilder().build(
        document,
        [(0, len(table)), (markdown_start, len(text))],
        ChunkPolicy(strategy="character", size=23),
    )
    later = [leaf for leaf in result.leaves if leaf.start >= markdown_start]
    assert len(later) == 2 and all(leaf.table for leaf in later)
    assert later[1].table.table_id == f"markdown-table:{markdown_start}"
    assert later[1].table.header_start == markdown_start
    assert later[1].index_text == "| 材料 |\n| --- |\n| 清单 |\n"


def test_unicode_table_newlines_do_not_create_phantom_rows_in_continuation_parents():
    separator = "\u2028"
    header = f"| A |{separator}| --- |{separator}"
    rows = [f"| {i} |{separator}" for i in range(8)]
    document = Document(id="unicode-table", text=header + "".join(rows))
    result = ParentChildBuilder().build_sections(
        document,
        ChunkPolicy(strategy="character", size=19, table_overflow="allow"),
        max_parent_chars=27,
    )
    assert len(result.parents) > 1
    assert [leaf.index_text for leaf in result.leaves] == [header + row for row in rows]
    assert [leaf.table.row_start for leaf in result.leaves] == list(range(len(rows)))
    assert all(leaf.table.header_end == len(header) for leaf in result.leaves)
    assert {warning.input_id for warning in result.warnings} == {leaf.id for leaf in result.leaves}


def test_rechunk_result_keeps_isolated_parse_configuration_and_business_metadata(tmp_path):
    from agent_components.chunking import Rechunker

    file = tmp_path / "terms.txt"
    file.write_text("签署后交付。", encoding="utf-8")
    parsed = DoclingParser().parse(file)
    parsed.document.metadata["labels"] = ["draft"]
    result = Rechunker().rechunk(parsed, ChunkPolicy(strategy="character"))
    parsed.provenance.options["encoding"] = "latin-1"
    parsed.document.metadata["labels"].append("later-edit")
    assert result.trace.parsing.options["encoding"] == "utf-8-sig"
    assert result.document.metadata["labels"] == ["draft"]
    assert not result.trace.matches_artifact(parsed)
