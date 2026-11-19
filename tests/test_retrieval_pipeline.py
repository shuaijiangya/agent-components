import pytest

from agent_components import retrieval
from agent_components.chunking import Chunker, ChunkPolicy, ParentChildBuilder
from agent_components.core.contracts import Document
from agent_components.retrieval import IndexRecord, LanceDBIndex


class QueryEmbedding:
    dimension = 2

    def __init__(self, vectors=((1.0, 0.0),)):
        self.vectors = vectors
        self.inputs = []

    def embed_batch(self, texts):
        self.inputs.extend(texts)
        return self.vectors


@pytest.fixture
def index(tmp_path):
    pytest.importorskip("lancedb")
    return LanceDBIndex(tmp_path / "index", dimensions=2)


def leaf(document_id, text, *, revision_id="1"):
    return (
        Chunker()
        .chunk(
            Document(id=document_id, text=text, revision_id=revision_id),
            ChunkPolicy(strategy="character"),
        )
        .leaves[0]
    )


def row(chunk, vector):
    return IndexRecord(
        id=chunk.id,
        document_id=chunk.document_id,
        revision_id=chunk.revision_id,
        text=chunk.index_text,
        vector=vector,
    )


@pytest.mark.integration
def test_dedup_parent_search_scopes_before_top_k(index):
    document = Document(id="contract", revision_id="2", text="付款期限。交付验收。")
    chunks = ParentChildBuilder().build(
        document,
        [(0, len(document.text))],
        ChunkPolicy(strategy="character", size=5),
    )
    hit, neighbor = chunks.leaves
    duplicate = leaf("duplicate", "付款期限。")
    old = leaf("contract", "上一版付款约定。")
    hidden = leaf("hidden", "内部付款金额。")
    index.upsert(
        [
            row(hit, (0.9, 0.0)),
            row(neighbor, (0.0, 1.0)),
            row(duplicate, (0.7, 0.0)),
            row(old, (1.0, 0.0)),
            row(hidden, (1.0, 0.0)),
        ]
    )
    provider = QueryEmbedding()
    result = retrieval.RetrievalPipeline(index, provider).search(
        "付款期限？",
        visible_chunks=[*chunks.chunks, duplicate],
        options=retrieval.RetrievalOptions(recall_limit=2, top_k=2, parent_mode="replace"),
    )

    assert provider.inputs == ["付款期限？"]
    assert [match.id for match in result.matches] == [hit.id]
    assert [(item.chunk_id, item.duplicate_of) for item in result.dropped] == [
        (duplicate.id, hit.id)
    ]
    assert result.context.selected_ids == (chunks.parents[0].id,)
    assert [window.text for window in result.context.windows] == ["付款期限。交付验收。"]


@pytest.mark.integration
def test_mmr_reads_index_vectors_and_expands_adjacent_visible_chunks(index):
    terms = Chunker().chunk(
        Document(id="terms", text="付款期限。交付验收。"),
        ChunkPolicy(strategy="character", size=5),
    )
    hit, neighbor = terms.leaves
    similar = leaf("similar", "付款条件与金额。")
    diverse = leaf("diverse", "保密及资料销毁。")
    index.upsert(
        [
            row(hit, (1.0, 0.0)),
            row(similar, (0.99, 0.01)),
            row(diverse, (0.6, 0.8)),
        ]
    )
    provider = QueryEmbedding()
    result = retrieval.RetrievalPipeline(index, provider).search(
        "合同约定",
        visible_chunks=[*terms.leaves, similar, diverse],
        options=retrieval.RetrievalOptions(
            selection="mmr",
            recall_limit=3,
            top_k=2,
            relevance_weight=0.3,
        ),
    )

    assert provider.inputs == ["合同约定"]
    assert [match.id for match in result.matches] == [hit.id, diverse.id]
    assert result.context.selected_ids == (hit.id, neighbor.id, diverse.id)
    assert [window.text for window in result.context.windows] == [
        "付款期限。交付验收。",
        "保密及资料销毁。",
    ]


@pytest.mark.integration
def test_empty_scope_returns_no_matches_without_embedding(index):
    hidden = leaf("private", "内部报价")
    index.upsert([row(hidden, (1.0, 0.0))])
    provider = QueryEmbedding(vectors=())
    result = retrieval.RetrievalPipeline(index, provider).search("报价", visible_chunks=[])
    assert result.matches == result.context.windows == ()
    assert provider.inputs == []


@pytest.mark.integration
@pytest.mark.parametrize("vectors", [(), ((1.0, 0.0), (0.0, 1.0)), ((0.0, 0.0),), ((1.0,),)])
def test_invalid_query_embeddings_fail_before_recall(index, vectors):
    with pytest.raises(ValueError, match="embedding"):
        retrieval.RetrievalPipeline(index, QueryEmbedding(vectors)).search(
            "付款",
            visible_chunks=[leaf("published", "付款期限。")],
        )


@pytest.mark.integration
def test_index_id_collision_cannot_return_stale_text(index):
    chunk = leaf("published", "付款期限。")
    index.upsert([row(chunk, (1.0, 0.0)).model_copy(update={"text": "内部报价"})])
    with pytest.raises(ValueError, match="visible chunk"):
        retrieval.RetrievalPipeline(index, QueryEmbedding()).search("付款", visible_chunks=[chunk])
