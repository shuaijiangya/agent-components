"""封装嵌入式 LanceDB 的索引原语。

权限、知识库版本发布和删除可见性应由上层独立组件管理。
"""

import math
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field, field_validator

from agent_components.core.contracts import Contract
from agent_components.core.errors import MissingDependencyError


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

    当前 score_kind 为 distance，分数越小表示向量距离越近。
    """

    id: str
    document_id: str
    revision_id: str
    text: str
    score: float
    score_kind: str


class LanceDBIndex:
    """提供本地进程内的向量索引写入和查询。

    不启动独立服务；本类还不是完整 RAG 或知识库生命周期组件。
    """

    def __init__(self, path: Path, *, dimensions: int, table_name: str = "chunks") -> None:
        """打开或创建固定维度的本地索引表。

        Args:
            path: LanceDB 数据目录。
            dimensions: 正整数向量维度。
            table_name: 索引表名称。

        Raises:
            ValueError: 维度无效或已有表结构不兼容。
            MissingDependencyError: 未安装 retrieval extra。
        """
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        try:
            import lancedb
            import pyarrow as pa
        except ImportError as exc:
            raise MissingDependencyError("retrieval") from exc
        self._dimensions = dimensions
        self._db = lancedb.connect(str(path))
        schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("document_id", pa.string()),
                pa.field("revision_id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dimensions)),
            ]
        )
        self._table = self._db.create_table(table_name, schema=schema, exist_ok=True)
        if self._table.schema != schema:
            raise ValueError("Existing LanceDB schema is incompatible with this index")

    def upsert(self, records: Sequence[IndexRecord]) -> None:
        """按条目 ID 原位更新或新增文本和向量。

        Args:
            records: 本批无重复 ID、向量维度匹配的记录。

        Raises:
            ValueError: 本批含重复 ID 或任一向量维度不匹配。
        """
        if not records:
            return
        if len({row.id for row in records}) != len(records):
            raise ValueError("Duplicate IDs in an upsert batch")
        if any(len(row.vector) != self._dimensions for row in records):
            raise ValueError("Embedding dimension does not match the index")
        rows = [row.model_dump(mode="json") for row in records]
        self._table.merge_insert(
            "id"
        ).when_matched_update_all().when_not_matched_insert_all().execute(rows)

    def build_fts(self) -> None:
        """构建或替换 text 字段的全文索引。

        仅建立后端索引；中文分词、BM25 查询和融合检索接口尚未封装。
        """
        self._table.create_fts_index("text", replace=True)

    def search_vector(
        self, vector: Sequence[float], *, limit: int = 10, document_ids: Sequence[str] | None = None
    ) -> tuple[IndexMatch, ...]:
        """按向量检索并可选预过滤文档身份。

        Args:
            vector: 长度匹配且元素有限的查询向量。
            limit: 返回条数上限，必须为正数。
            document_ids: 允许的文档 ID；None 不过滤，空序列直接返回空结果。

        Returns:
            后端排序的距离结果；该文档过滤不能代替权威权限和版本检查。

        Raises:
            ValueError: 向量无效或 limit 非正。
        """
        if len(vector) != self._dimensions or not all(math.isfinite(v) for v in vector):
            raise ValueError("Query vector has invalid dimension or values")
        if limit < 1:
            raise ValueError("limit must be positive")
        if document_ids is not None and not document_ids:
            return ()
        query = self._table.search(list(vector)).limit(limit)
        if document_ids is not None:
            values = ",".join("'" + value.replace("'", "''") + "'" for value in document_ids)
            query = query.where(f"document_id IN ({values})", prefilter=True)
        return tuple(
            IndexMatch(
                id=row["id"],
                document_id=row["document_id"],
                revision_id=row["revision_id"],
                text=row["text"],
                score=row["_distance"],
                score_kind="distance",
            )
            for row in query.to_list()
        )
