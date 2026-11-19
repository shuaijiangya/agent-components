"""将 LanceDB 嵌入式索引转换为 VectorStore 的公共契约。"""

import math
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from agent_components.core.errors import MissingDependencyError
from agent_components.retrieval.vector_store import IndexMatch, IndexRecord, VectorHit


class LanceDBVectorStore:
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
        # 运行时切换可能选中已打开的实例。每次读取检查其他写入者提交的新版本。
        self._db = lancedb.connect(str(path), read_consistency_interval=timedelta(0))
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

    @property
    def dimensions(self) -> int:
        """返回索引表定义的向量维度。"""
        return self._dimensions

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
        return tuple(
            hit.match for hit in self._search(vector, limit=limit, document_ids=document_ids)
        )

    def recall(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        chunk_ids: Sequence[str],
        include_vectors: bool,
    ) -> tuple[VectorHit, ...]:
        """在 Top K 前过滤可见块，按需从相同行读取候选向量。"""
        if chunk_ids is None:
            raise TypeError("filter IDs are required for scoped recall")
        return self._search(vector, limit=limit, chunk_ids=chunk_ids, with_vectors=include_vectors)

    def _search(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        document_ids: Sequence[str] | None = None,
        chunk_ids: Sequence[str] | None = None,
        with_vectors: bool = False,
    ) -> tuple[VectorHit, ...]:
        """为索引 API 和组装入口共享召回逻辑，按需返回同一行的向量。"""
        if len(vector) != self._dimensions or not all(math.isfinite(v) for v in vector):
            raise ValueError("Query vector has invalid dimension or values")
        if limit < 1:
            raise ValueError("limit must be positive")
        filters = []
        for column, identifiers in (("document_id", document_ids), ("id", chunk_ids)):
            if identifiers is None:
                continue
            if (
                not isinstance(identifiers, Sequence)
                or isinstance(identifiers, (str, bytes, bytearray))
                or any(not isinstance(value, str) for value in identifiers)
            ):
                raise TypeError("filter IDs must be a sequence of strings, such as a list or tuple")
            if not identifiers:
                return ()
            values = ",".join("'" + value.replace("'", "''") + "'" for value in identifiers)
            filters.append(f"{column} IN ({values})")
        query = self._table.search(list(vector)).limit(limit)
        if filters:
            query = query.where(" AND ".join(filters), prefilter=True)
        columns = ["id", "document_id", "revision_id", "text", "_distance"]
        if with_vectors:
            columns.append("vector")
        return tuple(
            VectorHit(
                match=IndexMatch(
                    id=row["id"],
                    document_id=row["document_id"],
                    revision_id=row["revision_id"],
                    text=row["text"],
                    score=row["_distance"],
                    score_kind="distance",
                ),
                vector=tuple(row["vector"]) if with_vectors else None,
            )
            for row in query.select(columns).to_list()
        )
