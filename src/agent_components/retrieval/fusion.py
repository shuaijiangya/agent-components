"""使用 LanceDB 的倒数排名融合合并检索通道。"""

from collections.abc import Sequence

from agent_components.core.errors import MissingDependencyError


def fuse_ranks(
    vector_ids: Sequence[str], keyword_ids: Sequence[str], *, k: int = 60
) -> tuple[tuple[str, float], ...]:
    """按通道内名次融合块 ID，每个 ID 在每个通道只计一次。

    同分时保留向量通道、关键词通道依次遍历的首次出现顺序。
    """
    try:
        import pyarrow as pa
        from lancedb.rerankers import RRFReranker
    except ImportError as exc:
        raise MissingDependencyError("retrieval") from exc

    vector_ids = tuple(dict.fromkeys(vector_ids))
    keyword_ids = tuple(dict.fromkeys(keyword_ids))
    chunk_ids = tuple(dict.fromkeys((*vector_ids, *keyword_ids)))
    row_ids = {chunk_id: row_id for row_id, chunk_id in enumerate(chunk_ids)}
    vector_rows = pa.table(
        {"_rowid": pa.array([row_ids[chunk_id] for chunk_id in vector_ids], type=pa.uint64())}
    )
    keyword_rows = pa.table(
        {"_rowid": pa.array([row_ids[chunk_id] for chunk_id in keyword_ids], type=pa.uint64())}
    )
    rows = RRFReranker(K=k).rerank_hybrid("", vector_rows, keyword_rows).to_pylist()
    rows.sort(key=lambda row: (-row["_relevance_score"], row["_rowid"]))
    return tuple((chunk_ids[row["_rowid"]], row["_relevance_score"]) for row in rows)
