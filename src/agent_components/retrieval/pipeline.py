"""将查询文本、向量召回和可见语料组装为一次检索操作。"""

import math
from collections.abc import Sequence
from typing import Literal, Self

from pydantic import Field, model_validator

from agent_components.chunking import EmbeddingProvider
from agent_components.core.contracts import Chunk, Contract
from agent_components.retrieval.context import ContextExpander, ContextResult
from agent_components.retrieval.selection import (
    RetrievalCandidate,
    SelectionDrop,
    deduplicate,
    mmr_select,
)
from agent_components.retrieval.vector_store import IndexMatch, VectorStore


class RetrievalOptions(Contract):
    """设置候选数量、选择策略以及上下文范围。

    recall_limit 是选择前的向量召回上限，top_k 是选择后的叶子块上限。
    relevance_weight 用于 MMR，dedup_threshold 用于文本去重。
    before 和 after 控制相邻叶子数量，parent_mode 控制父块补充方式。
    """

    recall_limit: int = Field(default=30, ge=1, le=2000, strict=True)
    top_k: int = Field(default=5, ge=1, strict=True)
    selection: Literal["dedup", "mmr"] = "dedup"
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


class RetrievalResult(Contract):
    """返回选中的命中、去重依据以及最终上下文。

    matches 保持选择后的顺序，score 和 score_kind 保留适配器提供的原始分值及方向。
    context 标注实际纳入和因字符预算省略的分块。
    """

    matches: tuple[IndexMatch, ...]
    dropped: tuple[SelectionDrop, ...] = ()
    context: ContextResult


class RetrievalPipeline:
    """通过注入的 Embedding Provider 和索引执行完整的向量检索链路。

    业务方传入当前请求已授权且已发布的 Chunk 集合，并管理 Provider 生命周期。
    一个调用只向量化查询文本，MMR 从召回行读取已有的候选向量。
    """

    def __init__(self, index: VectorStore, embedding: EmbeddingProvider) -> None:
        """绑定向量索引和查询向量提供方。

        Args:
            index: 已写入叶子块及其向量的向量存储适配器。
            embedding: 与索引数据使用同模型、同版本及预处理的同步向量提供方。

        Raises:
            ValueError: 提供方的向量维度与索引不一致。
        """
        if type(embedding.dimension) is not int or embedding.dimension != index.dimensions:
            raise ValueError("embedding dimension must match the index")
        self._index = index
        self._embedding = embedding
        self._context = ContextExpander()

    def search(
        self,
        query: str,
        *,
        visible_chunks: Sequence[Chunk],
        options: RetrievalOptions | None = None,
    ) -> RetrievalResult:
        """计算查询向量，召回叶子块，选择候选并扩展上下文。

        Args:
            query: 非空的业务查询原文。
            visible_chunks: 已按权限和发布版本过滤的数据。包含可召回的叶子及允许
                返回的父块、邻块。作用域为空时直接返回空结果。
            options: 召回与扩展设置，省略时使用文本去重及左右各一块邻居。

        Returns:
            选中的索引命中、去重记录、上下文窗口和预算省略信息。

        Raises:
            ValueError: 查询、可见语料、向量输出或索引行与语料的对应关系无效。
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
        leaf_ids = tuple(chunk.id for chunk in corpus if chunk.kind == "leaf")
        if not leaf_ids:
            return RetrievalResult(
                matches=(), context=ContextResult(windows=(), selected_ids=(), omitted_ids=())
            )

        vectors = self._embedding.embed_batch((query,))
        if len(vectors) != 1 or len(vectors[0]) != self._index.dimensions:
            raise ValueError("embedding must return one query vector with the index dimension")
        query_vector = tuple(vectors[0])
        norm = math.hypot(*query_vector)
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("query embedding must be finite and nonzero")

        # 适配器先按可见叶子过滤，再按原生分值召回至多 recall_limit 条候选。
        # 只有 MMR 需要返回候选向量。直接读取已入库的向量，无需重新计算。
        rows = self._index.recall(
            query_vector,
            limit=options.recall_limit,
            chunk_ids=leaf_ids,
            include_vectors=options.selection == "mmr",
        )
        if len(rows) > options.recall_limit:
            raise ValueError("vector store returned more than the requested limit")
        matches: dict[str, IndexMatch] = {}
        candidates = []
        for row in rows:
            match = row.match
            chunk = by_id.get(match.id)
            if (
                chunk is None
                or chunk.kind != "leaf"
                or (match.document_id, match.revision_id) != (chunk.document_id, chunk.revision_id)
                or match.text not in (chunk.text, chunk.index_text)
            ):
                raise ValueError("index row does not match the visible chunk")
            if match.score_kind not in {"distance", "similarity"}:
                raise ValueError("vector score_kind must be distance or similarity")
            relevance = -match.score if match.score_kind == "distance" else match.score
            matches[match.id] = match
            candidates.append(
                RetrievalCandidate(chunk=chunk, relevance=relevance, vector=row.vector)
            )
        # 去重和 MMR 都在这里执行，两个分支互斥。具体算法在 selection.py。
        # dedup 按适配器的召回顺序过滤重复文本，产出丢弃记录。
        # mmr 用查询向量和候选向量重新选择至多 top_k 条，不执行文本去重。
        selected = (
            mmr_select(
                candidates,
                query_vector,
                limit=options.top_k,
                relevance_weight=options.relevance_weight,
            )
            if options.selection == "mmr"
            else deduplicate(candidates, threshold=options.dedup_threshold)
        )
        # 去重可能保留多于 top_k 条，因此统一截取。只扩展最终入选的叶子块。
        hits = [item.chunk for item in selected.selected[: options.top_k]]
        context = self._context.expand(
            hits,
            corpus,
            before=options.before,
            after=options.after,
            parent_mode=options.parent_mode,
            stay_in_parent=options.stay_in_parent,
            max_characters=options.max_characters,
        )
        return RetrievalResult(
            matches=tuple(matches[hit.id] for hit in hits),
            dropped=selected.dropped,
            context=context,
        )
