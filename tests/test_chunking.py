import pytest

from agent_components.chunking import Chunker, ChunkPolicy, ParentChildBuilder
from agent_components.core.contracts import Document


def test_character_overlap_offsets_and_ids_are_deterministic():
    doc = Document(id="doc", text="合同条款第一部分\n合同条款第二部分", revision_id="r1")
    policy = ChunkPolicy(strategy="character", size=8, overlap=2)
    result = Chunker().chunk(doc, policy)
    assert result == Chunker().chunk(doc, policy)
    assert result.chunks[0].start == 0
    assert result.chunks[-1].end == len(doc.text)
    for chunk in result.chunks:
        assert chunk.text == doc.text[chunk.start : chunk.end]
    assert result.chunks[1].start == result.chunks[0].end - 2


def test_parent_child_ranges_cannot_cross_parents():
    doc = Document(id="doc", text="第一章abcdefgh第二章ijklmnop", revision_id="r1")
    result = ParentChildBuilder().build(
        doc, [(0, 11), (11, len(doc.text))], ChunkPolicy(strategy="character", size=4)
    )
    parents = {item.id: item for item in result.chunks if item.kind == "parent"}
    leaves = [item for item in result.chunks if item.kind == "leaf"]
    assert len(parents) == 2
    for leaf in leaves:
        parent = parents[leaf.parent_id]
        assert parent.start <= leaf.start < leaf.end <= parent.end
        assert doc.text[leaf.start : leaf.end] == leaf.text


def test_different_revisions_do_not_reuse_chunk_ids():
    one = Chunker().chunk(
        Document(id="same", text="内容", revision_id="r1"), ChunkPolicy(strategy="character")
    )
    two = Chunker().chunk(
        Document(id="same", text="内容", revision_id="r2"), ChunkPolicy(strategy="character")
    )
    assert one.chunks[0].id != two.chunks[0].id


def test_bad_overlap_and_parent_ranges_are_rejected():
    doc = Document(id="doc", text="abcdefgh")
    with pytest.raises(ValueError):
        Chunker().chunk(doc, ChunkPolicy(strategy="character", size=4, overlap=4))
    with pytest.raises(ValueError):
        ParentChildBuilder().build(doc, [(0, 5), (4, 8)], ChunkPolicy(strategy="character"))


@pytest.mark.integration
def test_real_chonkie_preserves_chinese_repeated_text_and_whitespace():
    pytest.importorskip("chonkie")
    text = "第一条：相同条款。\n\n第一条：相同条款。\n第二条：付款义务。" * 3
    result = Chunker().chunk(Document(id="real", text=text), ChunkPolicy(size=20))
    assert "".join(item.text for item in result.chunks) == text
    assert all(item.text == text[item.start : item.end] for item in result.chunks)


@pytest.mark.parametrize("strategy", ["character", "recursive"])
def test_empty_document_has_explicit_empty_coverage(strategy):
    result = Chunker().chunk(Document(id="empty", text=""), ChunkPolicy(strategy=strategy))
    assert result.chunks == result.parents == result.leaves == ()
    assert result.input_length == 0
    assert result.covered_ranges == ()


@pytest.mark.parametrize(
    "settings",
    [
        {"size": 0},
        {"size": -1},
        {"size": True},
        {"size": 3.0},
        {"size": "3"},
        {"overlap": -1},
        {"overlap": False},
        {"strategy": "unknown"},
        {"strategy": "character", "size": 3, "overlap": 3},
        {"delimiters": ("",)},
    ],
)
def test_policy_rejects_invalid_values_at_construction(settings):
    with pytest.raises(ValueError):
        ChunkPolicy(**settings)


def test_bypassed_policy_is_revalidated_even_for_empty_inputs():
    policy = ChunkPolicy(strategy="character", size=4).model_copy(update={"overlap": 4})
    doc = Document(id="empty", text="")
    with pytest.raises(ValueError):
        Chunker().chunk(doc, policy)
    with pytest.raises(ValueError):
        ParentChildBuilder().build(doc, [], policy)


def test_changed_content_does_not_reuse_ids_when_revision_was_not_updated():
    policy = ChunkPolicy(strategy="character", size=3)
    before = Chunker().chunk(Document(id="same", text="abcDEF"), policy)
    after = Chunker().chunk(Document(id="same", text="abcXYZ"), policy)
    assert before.config_fingerprint == after.config_fingerprint
    assert {c.id for c in before.chunks}.isdisjoint(c.id for c in after.chunks)


def test_ids_bind_document_policy_and_parent_layout():
    doc = Document(id="one", text="abcdefgh")
    policy = ChunkPolicy(strategy="character", size=4)
    result = Chunker().chunk(doc, policy)
    other_doc = Chunker().chunk(doc.model_copy(update={"id": "two"}), policy)
    other_policy = Chunker().chunk(doc, policy.model_copy(update={"size": 5}))
    assert result.chunks[0].id != other_doc.chunks[0].id
    assert result.chunks[0].id != other_policy.chunks[0].id
    assert result.config_fingerprint != other_policy.config_fingerprint
    first = ParentChildBuilder().build(doc, [(0, 4)], policy)
    second = ParentChildBuilder().build(doc, [(0, 8)], policy)
    assert first.config_fingerprint != second.config_fingerprint
    assert first.leaves[0].id != second.leaves[0].id


@pytest.mark.parametrize("text", [" ", "\n\n", "\r\n ", "  重复。重复。  ", "🙂中e\u0301\t文"])
def test_character_preserves_every_python_character(text):
    result = Chunker().chunk(
        Document(id="unicode", text=text), ChunkPolicy(strategy="character", size=2)
    )
    assert "".join(c.text for c in result.leaves) == text
    assert result.covered_ranges == ((0, len(text)),)
    assert result.input_length == len(text)
    assert all(0 < len(c.text) <= 2 for c in result.leaves)
    assert all(c.text == text[c.start : c.end] for c in result.leaves)


def test_maximum_character_overlap_progresses_and_preserves_final_boundary():
    result = Chunker().chunk(
        Document(id="overlap", text="abcdef"), ChunkPolicy(strategy="character", size=3, overlap=2)
    )
    assert [(c.start, c.end) for c in result.leaves] == [(0, 3), (1, 4), (2, 5), (3, 6)]
    assert result.covered_ranges == ((0, 6),)


def test_parent_offsets_ordinals_and_partial_coverage_are_explicit():
    doc = Document(id="selected", text="忽略ABCD跳过EFG尾部")
    result = ParentChildBuilder().build(
        doc, [(2, 6), (8, 11)], ChunkPolicy(strategy="character", size=2)
    )
    assert result.covered_ranges == ((2, 6), (8, 11))
    assert result.input_length == len(doc.text)
    assert [c.ordinal for c in result.parents] == [0, 1]
    assert [c.ordinal for c in result.leaves] == [0, 1, 2, 3]
    assert [c.ordinal_in_parent for c in result.leaves] == [0, 1, 0, 1]
    assert [c.kind for c in result.chunks] == ["parent", "leaf", "leaf", "parent", "leaf", "leaf"]
    for parent in result.parents:
        children = [c for c in result.leaves if c.parent_id == parent.id]
        assert "".join(c.text for c in children) == parent.text
        assert all(parent.start <= c.start < c.end <= parent.end for c in children)
        for child in children:
            assert child.source_refs[0].start == child.start
            assert child.source_refs[0].end == child.end
            assert doc.text[child.start : child.end] == child.text
    assert ParentChildBuilder().build(doc, [], ChunkPolicy(strategy="character")).chunks == ()


@pytest.mark.parametrize(
    "ranges",
    [
        [(0, 2), (1, 4)],
        [(2, 4), (0, 2)],
        [(-1, 2)],
        [(0, 9)],
        [(2, 2)],
        [(False, 2)],
        [(0.0, 2)],
        [(0,)],
        ["01"],
        [(0, 2), (4, 9)],
    ],
)
def test_parent_ranges_are_validated_before_any_child_work(ranges):
    class SpyChunker(Chunker):
        calls = 0

        def chunk(self, document, policy=None):
            self.calls += 1
            return super().chunk(document, policy)

    spy = SpyChunker()
    with pytest.raises(ValueError):
        ParentChildBuilder(spy).build(
            Document(id="bad", text="abcdefgh"), ranges, ChunkPolicy(strategy="character", size=2)
        )
    assert spy.calls == 0


def test_upstream_file_sources_are_not_misrepresented_as_chunk_sources():
    from agent_components.core.contracts import SourceRef

    doc = Document(
        id="file", text="abcdef", source_refs=(SourceRef(input_id="pdf", page=2, precision="page"),)
    )
    for result in (
        Chunker().chunk(doc, ChunkPolicy(strategy="character", size=2)),
        ParentChildBuilder().build(doc, [(2, 6)], ChunkPolicy(strategy="character", size=2)),
    ):
        assert [w.code for w in result.warnings] == ["source_mapping_unavailable"]
        for chunk in result.chunks:
            ref = chunk.source_refs[0]
            assert ref.page is None and ref.element_id is None
            assert ref.precision == "text" and ref.input_id == doc.id
            assert (ref.start, ref.end) == (chunk.start, chunk.end)


@pytest.mark.integration
@pytest.mark.parametrize(
    "text", [" ", "\n\n", "\r\n ", "  重复。重复。  ", "🙂中e\u0301\t文", "长" * 25]
)
def test_real_recursive_preserves_edge_cases_and_size_limit(text):
    pytest.importorskip("chonkie")
    result = Chunker().chunk(Document(id="edge", text=text), ChunkPolicy(size=3))
    assert "".join(c.text for c in result.leaves) == text
    assert all(0 < len(c.text) <= 3 for c in result.leaves)
    assert result.covered_ranges == ((0, len(text)),)


@pytest.mark.integration
@pytest.mark.parametrize(
    "native_chunks",
    [
        [],
        [(1, 3, "bc")],
        [(0, 2, "ab")],
        [(0, 4, "abcd")],
        [(0, 2, "xx")],
        [(0, 2, "ab"), (1, 3, "bc")],
        [(0, 0, ""), (0, 3, "abc")],
        [(False, 3, "abc")],
        [(0, 5, "abc")],
    ],
)
def test_recursive_rejects_broken_backend_output(monkeypatch, native_chunks):
    from types import SimpleNamespace

    chonkie = pytest.importorskip("chonkie")
    from agent_components.core.errors import ChunkAlignmentError

    output = [SimpleNamespace(start_index=a, end_index=b, text=t) for a, b, t in native_chunks]
    monkeypatch.setattr(chonkie.RecursiveChunker, "chunk", lambda self, text: output)
    with pytest.raises(ChunkAlignmentError):
        Chunker().chunk(Document(id="broken", text="abc"), ChunkPolicy(size=3))


def test_character_and_empty_recursive_do_not_import_chonkie(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "chonkie" or name.startswith("chonkie."):
            raise AssertionError("unexpected Chonkie import")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert (
        Chunker().chunk(Document(id="plain", text="abc"), ChunkPolicy(strategy="character")).leaves
    )
    assert not Chunker().chunk(Document(id="empty", text="")).chunks


def test_recursive_missing_dependency_has_actionable_error(monkeypatch):
    import builtins

    from agent_components.core.errors import MissingDependencyError

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "chonkie" or name.startswith("chonkie."):
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(MissingDependencyError, match=r"\[chunking\]"):
        Chunker().chunk(Document(id="recursive", text="abc"))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("甲乙丙\n\n丁戊己\n\n庚辛壬", ["甲乙丙\n\n", "丁戊己\n\n庚辛壬"]),
        ("甲乙丙丁。戊己庚辛。壬癸。", ["甲乙丙丁。", "戊己庚辛。壬癸。"]),
        ("aaa bbbb ccc", ["aaa ", "bbbb ccc"]),
    ],
)
def test_real_recursive_prefers_paragraph_sentence_and_whitespace_boundaries(text, expected):
    pytest.importorskip("chonkie")
    result = Chunker().chunk(Document(id="rules", text=text), ChunkPolicy(size=8))
    assert [c.text for c in result.leaves] == expected


@pytest.mark.integration
def test_real_recursive_parent_children_use_global_coordinates():
    pytest.importorskip("chonkie")
    doc = Document(id="parent-recursive", text="跳过首部甲乙丙丁。戊己庚辛。尾部")
    result = ParentChildBuilder().build(doc, [(4, 14)], ChunkPolicy(size=6))
    assert "".join(c.text for c in result.leaves) == doc.text[4:14]
    assert all(4 <= c.start < c.end <= 14 for c in result.leaves)
    assert all(doc.text[c.start : c.end] == c.text for c in result.leaves)
