import pytest

from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import Document
from agent_components.retrieval import (
    IndexRecord,
    LanceDBIndex,
    RetrievalOptions,
    RetrievalPipeline,
)


def _leaf(id, text, revision="r1"):
    return (
        Chunker()
        .chunk(Document(id=id, revision_id=revision, text=text), ChunkPolicy(strategy="character"))
        .leaves[0]
    )


def _record(chunk, vector):
    return IndexRecord(
        id=chunk.id,
        document_id=chunk.document_id,
        revision_id=chunk.revision_id,
        text=chunk.index_text,
        vector=vector,
    )


class QueryProvider:
    dimension = 2

    def embed_batch(self, texts):
        return [(1.0, 0.0)] * len(texts)


@pytest.mark.integration
def test_real_hybrid_returns_both_channel_scores_and_rerank_order(tmp_path):
    pytest.importorskip("lancedb")
    from agent_components.retrieval.reranking import RerankScore

    index = LanceDBIndex(tmp_path / "index", dimensions=2)
    vector_only = _leaf("vector", "收货时检查包装完整性。")
    both = _leaf("both", "设备验收不合格时重新交付。")
    keyword_only = _leaf("keyword", "验收验收：尾款在成功验收后支付。")
    private = _leaf("private", "验收验收验收；内部审批。")
    index.upsert(
        [
            _record(vector_only, (1.0, 0.0)),
            _record(both, (0.8, 0.2)),
            _record(keyword_only, (0.0, 1.0)),
            _record(private, (1.0, 0.0)),
        ]
    )

    class Reranker:
        def rerank(self, query, texts):
            assert query == "验收"
            assert set(texts) == {vector_only.text, both.text, keyword_only.text}
            return tuple(
                RerankScore(index=i, score=float(text == keyword_only.text))
                for i, text in enumerate(texts)
            )

    result = RetrievalPipeline(index, QueryProvider(), reranker=Reranker()).search(
        "验收",
        visible_chunks=[vector_only, both, keyword_only],
        options=RetrievalOptions(
            mode="hybrid",
            recall_limit=2,
            top_k=2,
            rerank=True,
            selection="exact",
            before=0,
            after=0,
        ),
    )
    assert result.matches[0].id == keyword_only.id
    assert result.hits[0].scores.rerank_score == 1
    assert result.hits[0].scores.bm25_score is not None
    assert result.hits[0].scores.vector_score is None
    fused = next(hit for hit in result.hits if hit.match.id == both.id)
    assert fused.scores.vector_score is not None and fused.scores.bm25_score is not None
    assert fused.scores.rrf_score > result.hits[0].scores.rrf_score
    assert result.hits[0].source_refs
    assert {window.document_id for window in result.context.windows} == {"keyword", "both"}


@pytest.mark.integration
def test_keyword_only_needs_no_embedding_and_explicit_rerank_fallback(tmp_path):
    pytest.importorskip("lancedb")
    from agent_components.retrieval.reranking import RerankerError

    index = LanceDBIndex(tmp_path / "index", dimensions=2)
    public = _leaf("p", "设备验收通过后支付尾款。")
    index.upsert([_record(public, (1.0, 0.0))])

    class Unavailable:
        def rerank(self, query, texts):
            raise RerankerError("timeout")

    pipeline = RetrievalPipeline(index, reranker=Unavailable())
    options = RetrievalOptions(mode="keyword", rerank=True)
    with pytest.raises(RerankerError):
        pipeline.search("尾款", visible_chunks=[public], options=options)
    result = pipeline.search(
        "尾款",
        visible_chunks=[public],
        options=options.model_copy(update={"reranker_failure": "fallback"}),
    )
    assert result.matches[0].id == public.id and result.matches[0].score_kind == "bm25"
    assert result.hits[0].scores.rerank_score is None
    assert [warning.code for warning in result.warnings] == ["reranker_fallback"]


@pytest.mark.integration
def test_hybrid_mmr_uses_stored_vectors_for_keyword_only_candidates(tmp_path):
    pytest.importorskip("lancedb")
    index = LanceDBIndex(tmp_path / "index", dimensions=2)
    semantic = _leaf("semantic", "采购部门负责签署与交付。")
    related = _leaf("related", "验收规则和流程。")
    keyword = _leaf("keyword", "验收通过后支付尾款。")
    index.upsert(
        [
            _record(semantic, (1.0, 0.0)),
            _record(related, (0.99, 0.01)),
            _record(keyword, (0.0, 1.0)),
        ]
    )
    result = RetrievalPipeline(index, QueryProvider()).search(
        "验收",
        visible_chunks=[semantic, related, keyword],
        options=RetrievalOptions(
            mode="hybrid",
            recall_limit=2,
            top_k=2,
            selection="mmr",
            relevance_weight=0.1,
        ),
    )
    assert [hit.match.id for hit in result.hits] == [related.id, keyword.id]
    assert result.hits[1].scores.vector_rank is None
    assert result.hits[1].scores.bm25_rank is not None
