"""将 LanceDB 嵌入式索引转换为 VectorStore 的公共契约。"""

import math
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from agent_components.core.errors import MissingDependencyError
from agent_components.retrieval.vector_store import IndexMatch, IndexRecord, VectorHit

_MATCH_COLUMNS = ("id", "document_id", "revision_id", "text")
_FTS_TOKENIZER = "icu"


class LanceDBVectorStore:
    """提供本地进程内的向量和全文索引写入与查询。

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
        """用 ICU 分词构建或刷新 text 字段的原生全文索引。"""
        from lancedb.index import FTS

        self._table.create_index(
            "text",
            config=FTS(base_tokenizer=_FTS_TOKENIZER, stem=False, remove_stop_words=False),
            replace=True,
        )

    def get_records(self, *, chunk_ids: Sequence[str]) -> tuple[IndexRecord, ...]:
        """按块 ID 读取现有记录，重复 ID 只返回一条。"""
        predicate = _ids_filter("id", chunk_ids)
        if predicate is None:
            return ()
        rows = self._table.search().where(predicate, prefilter=True).to_list()
        return tuple(IndexRecord.model_validate(row) for row in rows)

    def delete_records(self, *, chunk_ids: Sequence[str]) -> None:
        """删除指定块的记录，接受空范围和重复调用。"""
        predicate = _ids_filter("id", chunk_ids)
        if predicate is not None:
            self._table.delete(predicate)

    def recall_keywords(
        self, query: str, *, limit: int, chunk_ids: Sequence[str]
    ) -> tuple[IndexMatch, ...]:
        """在允许的块范围内执行原生中文分词和 BM25 召回。"""
        from lancedb.query import MatchQuery

        if limit < 1:
            raise ValueError("limit must be positive")
        predicate = _ids_filter("id", chunk_ids)
        if predicate is None or not query.strip():
            return ()
        if not self._has_keyword_index():
            try:
                self.build_fts()
            except RuntimeError:
                # 其他实例可能同时完成了索引创建。
                if not self._has_keyword_index():
                    raise
        # LanceDB 的 Pydantic dataclass 参数在运行时提供默认值。
        match_query = MatchQuery(query, "text")  # type: ignore[call-arg]
        rows = (
            self._table.search(match_query, query_type="fts")
            .where(predicate, prefilter=True)
            .limit(limit)
            .select([*_MATCH_COLUMNS, "_score"])
            .to_list()
        )
        return tuple(
            IndexMatch(
                id=row["id"],
                document_id=row["document_id"],
                revision_id=row["revision_id"],
                text=row["text"],
                score=row["_score"],
                score_kind="bm25",
            )
            for row in rows
        )

    def _has_keyword_index(self) -> bool:
        """检查当前表版本是否已有中文全文索引。"""
        return any(
            index.name == "text_idx"
            and index.index_type == "FTS"
            and index.columns == ["text"]
            and index.index_details is not None
            and index.index_details.get("base_tokenizer") == _FTS_TOKENIZER
            for index in self._table.list_indices()
        )

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
            predicate = _ids_filter(column, identifiers)
            if predicate is None:
                return ()
            filters.append(predicate)
        query = self._table.search(list(vector)).limit(limit)
        if filters:
            query = query.where(" AND ".join(filters), prefilter=True)
        columns = [*_MATCH_COLUMNS, "_distance"]
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


def _ids_filter(column: str, identifiers: Sequence[str]) -> str | None:
    """校验显式范围并生成转义后的 ID 过滤表达式。"""
    if (
        not isinstance(identifiers, Sequence)
        or isinstance(identifiers, (str, bytes, bytearray))
        or any(not isinstance(value, str) for value in identifiers)
    ):
        raise TypeError("filter IDs must be a sequence of strings, such as a list or tuple")
    if not identifiers:
        return None
    values = ",".join("'" + value.replace("'", "''") + "'" for value in dict.fromkeys(identifiers))
    return f"{column} IN ({values})"
