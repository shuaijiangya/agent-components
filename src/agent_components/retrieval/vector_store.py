"""定义向量存储端口和跨后端交换的索引数据。"""

import math
from collections.abc import Sequence
from typing import Protocol

from pydantic import Field, field_validator

from agent_components.core.contracts import Contract


class IndexRecord(Contract):
    """承载待索引文本及调用方计算的向量。

    向量必须非空且有限；维度匹配在索引写入时检查。
    """

    id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    text: str
    vector: tuple[float, ...] = Field(min_length=1)

    @field_validator("vector")
    @classmethod
    def finite_vector(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        """拒绝 NaN 或无穷大向量。

        Args:
            value: Pydantic 转换后的向量。

        Returns:
            全部元素有限的原向量。

        Raises:
            ValueError: 任一元素不是有限值。
        """
        if not all(math.isfinite(item) for item in value):
            raise ValueError("Embedding values must be finite")
        return value


class IndexMatch(Contract):
    """承载索引命中及分值语义。

    distance 分数越小表示距离越近，similarity 分数越大表示越相似。
    """

    id: str
    document_id: str
    revision_id: str
    text: str
    score: float
    score_kind: str


class VectorHit(Contract):
    """承载原始分值与按需返回的候选向量。

    向量来自与命中对应的同一条索引记录，供调用方执行 MMR。
    """

    match: IndexMatch
    vector: tuple[float, ...] | None = None


class VectorStore(Protocol):
    """约定向量写入和限定范围召回的跨后端接口。

    适配器负责把数据库返回的原生记录转换成公共契约。
    """

    @property
    def dimensions(self) -> int:
        """返回当前向量集合的维度。"""
        ...

    def upsert(self, records: Sequence[IndexRecord]) -> None:
        """按块 ID 写入向量记录。相同 ID 再写时更新对应记录。"""
        ...

    def recall(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        chunk_ids: Sequence[str],
        include_vectors: bool,
    ) -> tuple[VectorHit, ...]:
        """从允许的块中按相关度返回至多 limit 条命中。

        chunk_ids 为字符串列表或元组。适配器先应用范围再取 Top K，空范围返回空结果。
        score_kind 为 distance（越小越近）或 similarity（越大越近）。
        include_vectors 为真时，命中携带同一记录的候选向量。
        """
        ...
