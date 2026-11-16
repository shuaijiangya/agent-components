import pytest

from agent_components.chunking import Chunker, ChunkPolicy, ParentChildBuilder
from agent_components.context import ContextExpander
from agent_components.core.contracts import Document
from agent_components.selection import RetrievalCandidate, deduplicate, mmr_select


def candidates(texts, vectors):
    result = []
    for i, (text, vector) in enumerate(zip(texts, vectors, strict=True)):
        chunk = (
            Chunker()
            .chunk(Document(id=str(i), text=text), ChunkPolicy(strategy="character"))
            .leaves[0]
        )
        result.append(RetrievalCandidate(chunk=chunk, relevance=10 - i, vector=vector))
    return result


def test_mmr_trades_relevance_for_diverse_evidence_and_is_stable():
    items = candidates(
        ["付款条款A", "付款条款A的重复", "交付义务"], [(1, 0), (0.99, 0.01), (0.6, 0.8)]
    )
    relevance = mmr_select(items, [1, 0], limit=2, relevance_weight=1)
    diverse = mmr_select(items, [1, 0], limit=2, relevance_weight=0.3)
    assert [c.chunk.id for c in relevance.selected] == [items[0].chunk.id, items[1].chunk.id]
    assert [c.chunk.id for c in diverse.selected] == [items[0].chunk.id, items[2].chunk.id]
    assert diverse == mmr_select(items, [1, 0], limit=2, relevance_weight=0.3)


@pytest.mark.parametrize("vector", [[], [0, 0], [float("nan"), 0], [float("inf"), 0], [1]])
def test_mmr_rejects_invalid_query_or_dimensions(vector):
    items = candidates(["条款"], [(1, 0)])
    with pytest.raises(ValueError):
        mmr_select(items, vector)


def test_mmr_limits_candidate_work_and_requires_vectors():
    items = candidates(["a", "b"], [(1, 0), (0, 1)])
    with pytest.raises(ValueError):
        mmr_select(items, [1, 0], max_candidates=1)
    with pytest.raises(ValueError):
        mmr_select([items[0].model_copy(update={"vector": None})], [1, 0])
    with pytest.raises(ValueError):
        mmr_select(items, [1, 0], relevance_weight=float("nan"))


def test_exact_and_chinese_near_duplicate_decisions_are_traceable():
    items = candidates(
        [
            "  Ａ公司付款金额为3200元。",
            "a公司付款金额为3200元。",
            "a公司付款金额为3201元。",
            "物流交付必须完成验收。",
        ],
        [None] * 4,
    )
    result = deduplicate(items, threshold=0.6, shingle_size=2)
    assert [i.chunk.id for i in result.selected] == [items[0].chunk.id, items[3].chunk.id]
    assert [d.reason for d in result.dropped] == ["normalized_exact", "shingle_jaccard"]
    assert all(d.duplicate_of == items[0].chunk.id for d in result.dropped)
    assert deduplicate([], threshold=1).selected == ()


def test_expansion_stays_within_document_version_and_parent_then_merges_overlap():
    doc = Document(id="d", revision_id="r1", text="abcdefghijklmnopqrstuvwxyz")
    result = ParentChildBuilder().build(
        doc, [(0, 13), (13, 26)], ChunkPolicy(strategy="character", size=5, overlap=2)
    )
    other = Chunker().chunk(
        doc.model_copy(update={"revision_id": "r2"}), ChunkPolicy(strategy="character", size=5)
    )
    hit = result.leaves[2]
    context = ContextExpander().expand([hit], [*result.chunks, *other.chunks], before=2, after=20)
    assert len(context.windows) == 1
    window = context.windows[0]
    assert window.document_id == "d" and window.revision_id == "r1"
    assert window.text == doc.text[window.start : window.end]
    assert window.start == 0 and window.end == 13
    assert len(window.text) < sum(
        len(c.text) for c in result.leaves if c.parent_id == hit.parent_id
    )


@pytest.mark.parametrize("mode", ["append", "replace"])
def test_parent_retrieval_supplements_full_chapter_and_removes_duplicate_text(mode):
    doc = Document(id="parent", text="第一章abcdefghij第二章klmnop")
    result = ParentChildBuilder().build(
        doc, [(0, 13), (13, len(doc.text))], ChunkPolicy(strategy="character", size=4)
    )
    expanded = ContextExpander().expand([result.leaves[1]], result.chunks, parent_mode=mode)
    assert len(expanded.windows) == 1
    assert expanded.windows[0].text == result.parents[0].text


def test_neighbor_expansion_stops_at_invisible_gap_and_budget_is_reported():
    result = Chunker().chunk(
        Document(id="gap", text="abcdefghijklmnopqrst"), ChunkPolicy(strategy="character", size=4)
    )
    visible = [result.leaves[0], *result.leaves[2:]]
    expanded = ContextExpander().expand([visible[0]], visible, before=0, after=10)
    assert expanded.selected_ids == (visible[0].id,)
    limited = ContextExpander().expand([result.leaves[0]], result.chunks, after=3, max_characters=4)
    assert limited.windows[0].text == "abcd"
    assert limited.omitted_ids
    assert sum(len(w.index_text) for w in limited.windows) <= 4


def test_expansion_rejects_out_of_corpus_hits_missing_parents_and_conflicting_overlap():
    doc = Document(id="c", text="abcdefghij")
    result = ParentChildBuilder().build(
        doc, [(0, 10)], ChunkPolicy(strategy="character", size=4, overlap=1)
    )
    with pytest.raises(ValueError, match="visible"):
        ContextExpander().expand([result.leaves[0]], [])
    with pytest.raises(ValueError, match="parent"):
        ContextExpander().expand([result.leaves[0]], result.leaves, parent_mode="replace")
    broken = result.leaves[1].model_copy(update={"text": "xxxx"})
    with pytest.raises(ValueError, match="inconsistent"):
        ContextExpander().expand([result.leaves[0]], [result.leaves[0], broken], after=1)
