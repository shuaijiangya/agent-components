"""组合限定范围的向量、关键词检索，返回各阶段分数和上下文。"""

import math
from collections.abc import Sequence
from typing import Literal, Self

from pydantic import Field, model_validator

from agent_components.chunking import EmbeddingProvider
from agent_components.core.contracts import Chunk, Contract, Issue, SourceRef
from agent_components.models.providers.reranking import RerankerError, RerankerProvider
from agent_components.retrieval.context import ContextExpander, ContextResult
from agent_components.retrieval.fusion import fuse_ranks
from agent_components.retrieval.selection import (
    RetrievalCandidate,
    SelectionDrop,
    deduplicate,
    exact_deduplicate,
    mmr_select,
)
from agent_components.retrieval.vector_store import IndexMatch, KeywordStore, VectorHit, VectorStore


class RetrievalOptions(Contract):
    """配置召回通道、候选选择和上下文扩展。

    recall_limit 是每个召回通道的候选上限，top_k 是最终叶子命中数。
    rerank 通过应用注入的 Provider 执行，失败时默认抛出异常。
    """

    mode: Literal["vector", "keyword", "hybrid"] = "vector"
    recall_limit: int = Field(default=30, ge=1, le=2000, strict=True)
    top_k: int = Field(default=5, ge=1, strict=True)
    selection: Literal["dedup", "exact", "mmr"] = "dedup"
    rrf_k: int = Field(default=60, ge=1, strict=True)
    rerank: bool = False
    reranker_failure: Literal["raise", "fallback"] = "raise"
    relevance_weight: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    dedup_threshold: float = Field(default=0.85, gt=0, le=1, allow_inf_nan=False)
    before: int = Field(default=1, ge=0, strict=True)
    after: int = Field(default=1, ge=0, strict=True)
    parent_mode: Literal["none", "append", "replace"] = "none"
    stay_in_parent: bool = True
    max_characters: int = Field(default=8000, ge=1, strict=True)

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        """确保选择上限处于召回预算内。"""
        if self.top_k > self.recall_limit:
            raise ValueError("top_k must not exceed recall_limit")
        return self


class RetrievalScores(Contract):
    """保留每个通道的原始分值和排序，缺失字段表示该阶段未命中。"""

    vector_score: float | None = None
    vector_score_kind: str | None = None
    vector_rank: int | None = None
    bm25_score: float | None = None
    bm25_rank: int | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None


class SearchHit(Contract):
    """返回命中块、分阶段得分及精确的原始块来源。"""

    match: IndexMatch
    scores: RetrievalScores
    source_refs: tuple[SourceRef, ...]


class RetrievalResult(Contract):
    """区分选中的叶子命中与用于回答的上下文窗口。"""

    matches: tuple[IndexMatch, ...]
    dropped: tuple[SelectionDrop, ...] = ()
    context: ContextResult
    hits: tuple[SearchHit, ...] = ()
    warnings: tuple[Issue, ...] = ()


def _check_matches(
    rows: Sequence[IndexMatch], by_id: dict[str, Chunk], limit: int, score_kinds: set[str]
) -> dict[str, IndexMatch]:
    if len(rows) > limit or len({row.id for row in rows}) != len(rows):
        raise ValueError("retrieval backend returned too many or duplicate hits")
    for match in rows:
        chunk = by_id.get(match.id)
        if (
            chunk is None
            or chunk.kind != "leaf"
            or (match.document_id, match.revision_id) != (chunk.document_id, chunk.revision_id)
            or match.text not in (chunk.text, chunk.index_text)
        ):
            raise ValueError("index row does not match the visible chunk")
        if match.score_kind not in score_kinds or not math.isfinite(match.score):
            raise ValueError("retrieval backend returned an invalid score")
    return {row.id: row for row in rows}


class RetrievalPipeline:
    """从调用方授权的领域分块召回，再融合、重排、去重和扩展。

    可直接传入独立的关键词适配器，或使用向量索引同时提供的关键词端口。
    只启用关键词召回且选择非 MMR 时，查询无需向量化。
    """

    def __init__(
        self,
        index: VectorStore,
        embedding: EmbeddingProvider | None = None,
        *,
        keyword_index: KeywordStore | None = None,
        reranker: RerankerProvider | None = None,
    ) -> None:
        """配置可替换召回存储和模型提供方。"""
        if embedding is not None and (
            type(embedding.dimension) is not int or embedding.dimension != index.dimensions
        ):
            raise ValueError("embedding dimension must match the index")
        self._index = index
        self._embedding = embedding
        self._keyword = (
            keyword_index
            if keyword_index is not None
            else (index if isinstance(index, KeywordStore) else None)
        )
        self._reranker = reranker
        self._context = ContextExpander()

    def _query_vector(self, query: str) -> tuple[float, ...]:
        if self._embedding is None:
            raise ValueError("vector retrieval and MMR require an embedding provider")
        vectors = self._embedding.embed_batch((query,))
        if len(vectors) != 1 or len(vectors[0]) != self._index.dimensions:
            raise ValueError("embedding must return one query vector with the index dimension")
        query_vector = tuple(vectors[0])
        norm = math.hypot(*query_vector)
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("query embedding must be finite and nonzero")
        return query_vector

    def _rerank(
        self, query: str, ids: list[str], by_id: dict[str, Chunk], options: RetrievalOptions
    ) -> tuple[list[str], dict[str, float], tuple[Issue, ...]]:
        if not options.rerank or not ids:
            return ids, {}, ()
        if self._reranker is None:
            raise ValueError("rerank=True requires a reranker provider")
        try:
            response = self._reranker.rerank(query, [by_id[id].index_text for id in ids])
            by_index = {item.index: item.score for item in response}
            if (
                len(response) != len(ids)
                or set(by_index) != set(range(len(ids)))
                or any(not math.isfinite(score) for score in by_index.values())
            ):
                raise RerankerError("invalid_response")
        except RerankerError as exc:
            if options.reranker_failure == "raise":
                raise
            return (
                ids,
                {},
                (Issue(code="reranker_fallback", message=f"Reranker failed ({exc.reason})"),),
            )
        scores = {id: by_index[index] for index, id in enumerate(ids)}
        return sorted(ids, key=scores.__getitem__, reverse=True), scores, ()

    def search(
        self,
        query: str,
        *,
        visible_chunks: Sequence[Chunk],
        options: RetrievalOptions | None = None,
        allow_missing_parents: bool = False,
    ) -> RetrievalResult:
        """先限制到可见叶子再召回，返回文档版本、来源和分阶段得分。

        visible_chunks 是请求内的权限与版本快照，包含可用的父块和相邻块。
        管理型知识库通过它的 search 入口在返回前复核这份快照。
        """
        if not query.strip():
            raise ValueError("query must not be blank")
        options = (
            RetrievalOptions.model_validate(options.model_dump()) if options else RetrievalOptions()
        )
        corpus = tuple(visible_chunks)
        by_id = {chunk.id: chunk for chunk in corpus}
        if len(by_id) != len(corpus):
            raise ValueError("visible chunk IDs must be unique")
        if options.mode != "vector" and self._keyword is None:
            raise ValueError("keyword or hybrid retrieval requires a keyword store")
        if options.rerank and self._reranker is None:
            raise ValueError("rerank=True requires a reranker provider")
        leaf_ids = tuple(chunk.id for chunk in corpus if chunk.kind == "leaf")
        if not leaf_ids:
            return RetrievalResult(
                matches=(), context=ContextResult(windows=(), selected_ids=(), omitted_ids=())
            )

        query_vector = (
            self._query_vector(query)
            if options.mode != "keyword" or options.selection == "mmr"
            else ()
        )
        vector_rows: tuple[VectorHit, ...] = ()
        keyword_rows: tuple[IndexMatch, ...] = ()
        # 每个通道都在数据库 Top K 前应用相同的允许范围。
        if options.mode != "keyword":
            vector_rows = self._index.recall(
                query_vector,
                limit=options.recall_limit,
                chunk_ids=leaf_ids,
                include_vectors=options.selection == "mmr",
            )
        if options.mode != "vector":
            assert self._keyword is not None
            keyword_rows = self._keyword.recall_keywords(
                query, limit=options.recall_limit, chunk_ids=leaf_ids
            )
        vector = _check_matches(
            [hit.match for hit in vector_rows],
            by_id,
            options.recall_limit,
            {"distance", "similarity"},
        )
        keyword = _check_matches(keyword_rows, by_id, options.recall_limit, {"bm25"})
        matches = keyword | vector
        vector_ranks = {id: i for i, id in enumerate(vector, 1)}
        keyword_ranks = {id: i for i, id in enumerate(keyword, 1)}
        rrf = (
            dict(fuse_ranks(tuple(vector), tuple(keyword), k=options.rrf_k))
            if options.mode == "hybrid"
            else {}
        )
        ids = list(rrf if options.mode == "hybrid" else matches)
        ids, reranked, warnings = self._rerank(query, ids, by_id, options)

        vectors = {hit.match.id: hit.vector for hit in vector_rows if hit.vector is not None}
        if options.selection == "mmr":
            missing = tuple(id for id in ids if id not in vectors)
            if missing:
                # 混合检索里的纯关键词命中也要参与多样性比较，按 ID 取回已有向量。
                extras = self._index.recall(
                    query_vector, limit=len(missing), chunk_ids=missing, include_vectors=True
                )
                extra_by_id = {id: by_id[id] for id in missing}
                _check_matches(
                    [hit.match for hit in extras],
                    extra_by_id,
                    len(missing),
                    {"distance", "similarity"},
                )
                vectors.update(
                    {hit.match.id: hit.vector for hit in extras if hit.vector is not None}
                )
        use_ranking = options.mode != "vector" or bool(reranked)
        candidates = [
            RetrievalCandidate(
                chunk=by_id[id], relevance=1 - pos / max(1, len(ids) - 1), vector=vectors.get(id)
            )
            for pos, id in enumerate(ids)
        ]
        limit = 2 * options.recall_limit if options.mode == "hybrid" else options.recall_limit
        if options.selection == "mmr":
            selected = mmr_select(
                candidates,
                query_vector,
                limit=options.top_k,
                relevance_weight=options.relevance_weight,
                use_candidate_relevance=use_ranking,
                max_candidates=limit,
            )
        elif options.selection == "exact":
            selected = exact_deduplicate(candidates, max_candidates=limit)
        else:
            selected = deduplicate(
                candidates, threshold=options.dedup_threshold, max_candidates=limit
            )
        chunks = [item.chunk for item in selected.selected[: options.top_k]]
        hits = []
        for chunk in chunks:
            id = chunk.id
            semantic, lexical = vector.get(id), keyword.get(id)
            hits.append(
                SearchHit(
                    match=matches[id],
                    source_refs=chunk.source_refs,
                    scores=RetrievalScores(
                        vector_score=semantic.score if semantic else None,
                        vector_score_kind=semantic.score_kind if semantic else None,
                        vector_rank=vector_ranks.get(id),
                        bm25_score=lexical.score if lexical else None,
                        bm25_rank=keyword_ranks.get(id),
                        rrf_score=rrf.get(id),
                        rerank_score=reranked.get(id),
                    ),
                )
            )
        context = self._context.expand(
            chunks,
            corpus,
            before=options.before,
            after=options.after,
            parent_mode=options.parent_mode,
            stay_in_parent=options.stay_in_parent,
            max_characters=options.max_characters,
            allow_missing_parents=allow_missing_parents,
        )
        return RetrievalResult(
            matches=tuple(hit.match for hit in hits),
            context=context,
            dropped=selected.dropped,
            hits=tuple(hits),
            warnings=warnings,
        )
