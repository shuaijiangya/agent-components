"""提供召回后的 MMR、精确及近重复去重组件。

不调用模型或数据库；调用方提供已经过权限与版本过滤的候选及实际向量。
"""

import math
import unicodedata
from collections.abc import Sequence

from pydantic import Field

from agent_components.core.contracts import Chunk, Contract


class RetrievalCandidate(Contract):
    """承载召回候选及可选向量。

    relevance 越大越相关；MMR 使用查询/候选余弦，不直接混合未归一化的 BM25 分数。
    """

    chunk: Chunk
    relevance: float = Field(allow_inf_nan=False)
    vector: tuple[float, ...] | None = None


class SelectionDrop(Contract):
    """记录被去重的候选和保留依据。

    similarity 为规范化文本的 Jaccard；精确重复时为一。
    """

    chunk_id: str
    duplicate_of: str
    similarity: float
    reason: str


class SelectionResult(Contract):
    """返回保留候选与可追溯的去重决定。

    selected 保持输入排序，MMR 的输出则按贪心选择顺序排列。
    """

    selected: tuple[RetrievalCandidate, ...]
    dropped: tuple[SelectionDrop, ...] = ()


def _check_candidates(candidates: Sequence[RetrievalCandidate], maximum: int) -> None:
    if type(maximum) is not int or maximum < 1 or len(candidates) > maximum:
        raise ValueError("candidate count exceeds a positive configured maximum")
    if len({item.chunk.id for item in candidates}) != len(candidates):
        raise ValueError("candidate IDs must be unique")


def _unit(vector: Sequence[float]) -> tuple[float, ...]:
    if len(vector) == 0 or not all(math.isfinite(v) for v in vector):
        raise ValueError("vectors must be nonempty and finite")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("vectors must have a finite nonzero norm")
    return tuple(v / norm for v in vector)


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True))))


def mmr_select(
    candidates: Sequence[RetrievalCandidate],
    query_vector: Sequence[float],
    *,
    limit: int = 10,
    relevance_weight: float = 0.5,
    max_candidates: int = 2000,
    use_candidate_relevance: bool = False,
) -> SelectionResult:
    """执行有界候选集上的最大边际相关性选择。

    Args:
        candidates: 已过滤可见性、ID 唯一且带向量的候选序列。
        query_vector: 与候选使用同一模型和维度的查询向量。
        limit: 最多保留数量。
        relevance_weight: lambda，范围为零到一。
        max_candidates: 拒绝超大候选池，限制 O(n*k) 比较工作量。
        use_candidate_relevance: 使用上游已归一化的融合或重排相关度。

    Returns:
        按 lambda*cos(query,item)-(1-lambda)*max_cos(item,selected) 选择的结果。
        同分时保留输入顺序，不调用额外模型。

    Raises:
        ValueError: 参数、候选身份、向量值或维度无效。
    """
    _check_candidates(candidates, max_candidates)
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    if not math.isfinite(relevance_weight) or not 0 <= relevance_weight <= 1:
        raise ValueError("relevance_weight must be between zero and one")
    # 将查询和候选向量转换为单位向量，之后用点积计算余弦相似度。
    query = _unit(query_vector)
    vectors = []
    for item in candidates:
        if item.vector is None or len(item.vector) != len(query):
            raise ValueError("MMR requires equal-dimensional candidate vectors")
        vectors.append(_unit(item.vector))
    # relevance[i] 用查询与候选的余弦相似度计算，与 LanceDB 返回的距离分数无关。
    relevance = (
        [item.relevance for item in candidates]
        if use_candidate_relevance
        else [_cosine(query, vector) for vector in vectors]
    )
    if use_candidate_relevance and any(not 0 <= score <= 1 for score in relevance):
        raise ValueError("upstream MMR relevance must be normalized to [0, 1]")
    remaining = list(range(len(candidates)))
    selected: list[int] = []
    redundancy = [0.0] * len(candidates)
    while remaining and len(selected) < limit:
        # 这里选出本轮入选的候选。第一轮直接取查询相关性最高的块。
        # 后续得分 = 相关性权重 × 查询相似度 − 多样性权重 × 与已选块的最大相似度。
        # 相似内容会受到惩罚，但这里没有判定重复的阈值，候选仍可能入选。
        best = max(
            remaining,
            key=lambda i: (
                relevance[i]
                if not selected
                else relevance_weight * relevance[i] - (1 - relevance_weight) * redundancy[i]
            ),
        )
        remaining.remove(best)
        selected.append(best)
        # 为下一轮更新每个剩余候选与已选集合的最大相似度。
        # 第一次直接使用本轮相似度，以保留余弦为负的情况。
        for index in remaining:
            value = _cosine(vectors[index], vectors[best])
            redundancy[index] = value if len(selected) == 1 else max(redundancy[index], value)
    return SelectionResult(selected=tuple(candidates[i] for i in selected))


def exact_deduplicate(
    candidates: Sequence[RetrievalCandidate], *, max_candidates: int = 2000
) -> SelectionResult:
    """按排序去除重复文本及同文档版本的完全相同原文区间。"""
    _check_candidates(candidates, max_candidates)
    seen: dict[tuple[str, str, int, int, str], str] = {}
    exact: dict[str, str] = {}
    kept = []
    dropped = []
    for item in candidates:
        chunk = item.chunk
        position = (
            chunk.document_id,
            chunk.revision_id,
            chunk.start,
            chunk.end,
            chunk.context_prefix,
        )
        normalized = _normalized(chunk)
        duplicate = seen.get(position) or exact.get(normalized)
        if duplicate is not None:
            dropped.append(
                SelectionDrop(
                    chunk_id=chunk.id,
                    duplicate_of=duplicate,
                    similarity=1,
                    reason="source_interval" if position in seen else "normalized_exact",
                )
            )
            continue
        seen[position] = exact[normalized] = chunk.id
        kept.append(item)
    return SelectionResult(selected=tuple(kept), dropped=tuple(dropped))


def _normalized(chunk: Chunk) -> str:
    return " ".join(unicodedata.normalize("NFKC", chunk.index_text).casefold().split())


def deduplicate(
    candidates: Sequence[RetrievalCandidate],
    *,
    threshold: float = 0.85,
    shingle_size: int = 3,
    max_candidates: int = 2000,
) -> SelectionResult:
    """按排序保留首次出现的精确或近重复正文。

    Args:
        candidates: 已排序且 ID 唯一的候选，通常由高相关到低相关。
        threshold: 字符 shingles 的 Jaccard 阈值，范围为大于零到一。
        shingle_size: 规范文本的字符 n-gram 长度，默认兼容无空格中文。
        max_candidates: 候选上限，限制两两比较的资源消耗。

    Returns:
        保留候选及每个去重候选的参照身份、相似度和原因。

    Raises:
        ValueError: 阈值、shingle 大小或候选集合无效。
    """
    _check_candidates(candidates, max_candidates)
    if not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1]")
    if type(shingle_size) is not int or shingle_size < 1:
        raise ValueError("shingle_size must be a positive integer")
    kept: list[RetrievalCandidate] = []
    signatures: list[set[str]] = []
    exact: dict[str, str] = {}
    dropped = []
    # 按召回顺序扫描。只有已保留的候选才会作为后续比较的参照。
    for item in candidates:
        # 比较 index_text（上下文前缀加正文），统一全半角、大小写和连续空白。
        normalized = _normalized(item.chunk)
        # 精确去重在这里判定。规范化文本相同时，保留先入选的块，标记当前块为重复项。
        if normalized in exact:
            dropped.append(
                SelectionDrop(
                    chunk_id=item.chunk.id,
                    duplicate_of=exact[normalized],
                    similarity=1,
                    reason="normalized_exact",
                )
            )
            continue
        # 近重复比较用连续字符片段的集合。按默认的三字符长度，
        # “付款期限”得到“付款期”和“款期限”两个片段。
        grams = {
            normalized[i : i + shingle_size]
            for i in range(max(1, len(normalized) - shingle_size + 1))
        }
        duplicate = None
        for index, signature in enumerate(signatures):
            # 交集最多等于较小集合的长度，并集至少等于较大集合的长度。
            # 若按这个上限算也达不到阈值，就跳过这组比较。
            if min(len(grams), len(signature)) / max(len(grams), len(signature)) < threshold:
                continue
            # 这里判定近重复。Jaccard = 片段交集的大小 / 片段并集的大小。
            # 达到阈值就关联第一个满足条件的已保留块。
            similarity = len(grams & signature) / len(grams | signature)
            if similarity >= threshold:
                duplicate = SelectionDrop(
                    chunk_id=item.chunk.id,
                    duplicate_of=kept[index].chunk.id,
                    similarity=similarity,
                    reason="shingle_jaccard",
                )
                break
        # 两种检查都没命中才保留当前块，并让它参与后续比较。
        if duplicate is None:
            exact[normalized] = item.chunk.id
            kept.append(item)
            signatures.append(grams)
        else:
            dropped.append(duplicate)
    return SelectionResult(selected=tuple(kept), dropped=tuple(dropped))
