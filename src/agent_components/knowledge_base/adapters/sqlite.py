"""以 SQLite 持久化知识库内容与发布指针。"""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from itertools import batched
from pathlib import Path

from agent_components.core.errors import RevisionConflictError
from agent_components.knowledge_base.contracts import KnowledgeRevision
from agent_components.knowledge_base.repository import _document_scope


def _encode_revision(revision: KnowledgeRevision) -> str:
    # 统一键顺序，同时保留 true 与 1 等 Python 相等、JSON 类型不同的值。
    return json.dumps(
        revision.model_dump(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class SQLiteKnowledgeRepository:
    """使用本地 SQLite 文件存储版本快照。

    每次操作独立获取并关闭连接。写事务串行校验发布指针与预期版本。
    """

    def __init__(self, path: Path) -> None:
        """打开或初始化知识库内容表。"""
        self._path = path.resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction(write=True) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS kb_document_revisions (
                    knowledge_base_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (knowledge_base_id, document_id, revision_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS kb_published_documents (
                    knowledge_base_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    PRIMARY KEY (knowledge_base_id, document_id),
                    FOREIGN KEY (knowledge_base_id, document_id, revision_id)
                        REFERENCES kb_document_revisions
                            (knowledge_base_id, document_id, revision_id)
                )
            """)

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=5)) as db, db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    @staticmethod
    def _revision_row(
        db: sqlite3.Connection, knowledge_base_id: str, document_id: str, revision_id: str
    ) -> str | None:
        row = db.execute(
            "SELECT payload FROM kb_document_revisions "
            "WHERE knowledge_base_id = ? AND document_id = ? AND revision_id = ?",
            (knowledge_base_id, document_id, revision_id),
        ).fetchone()
        return None if row is None else str(row[0])

    def save_revision(self, knowledge_base_id: str, revision: KnowledgeRevision) -> None:
        """提交不可覆盖的版本。重复写入相同版本快照时直接返回。"""
        # JSON 可能把 NaN 或无穷大变成 null。先验证读回的快照，再开启写事务。
        try:
            payload = _encode_revision(revision)
            restored = KnowledgeRevision.model_validate_json(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("knowledge revision must round-trip through JSON") from exc
        if _encode_revision(restored) != payload:
            raise ValueError("knowledge revision must round-trip through JSON without loss")
        key = (knowledge_base_id, revision.document.id, revision.document.revision_id)
        with self._transaction(write=True) as db:
            existing = self._revision_row(db, *key)
            if existing is not None:
                if existing != payload:
                    raise RevisionConflictError("knowledge revision already has different content")
                return
            db.execute(
                "INSERT INTO kb_document_revisions VALUES (?, ?, ?, ?)",
                (*key, payload),
            )

    def get_revision(
        self, knowledge_base_id: str, document_id: str, revision_id: str
    ) -> KnowledgeRevision | None:
        """恢复一个指定版本的公共数据契约。"""
        with self._transaction() as db:
            payload = self._revision_row(db, knowledge_base_id, document_id, revision_id)
        return None if payload is None else KnowledgeRevision.model_validate_json(payload)

    def publish_revision(
        self,
        knowledge_base_id: str,
        document_id: str,
        revision_id: str,
        *,
        expected_revision: str | None,
    ) -> None:
        """在同一写事务中校验当前发布版本、目标是否存在并更新指针。"""
        key = (knowledge_base_id, document_id)
        with self._transaction(write=True) as db:
            row = db.execute(
                "SELECT revision_id FROM kb_published_documents "
                "WHERE knowledge_base_id = ? AND document_id = ?",
                key,
            ).fetchone()
            if (None if row is None else row[0]) != expected_revision:
                raise RevisionConflictError("published revision no longer matches")
            exists = db.execute(
                "SELECT 1 FROM kb_document_revisions "
                "WHERE knowledge_base_id = ? AND document_id = ? AND revision_id = ?",
                (*key, revision_id),
            ).fetchone()
            if exists is None:
                raise LookupError("knowledge revision does not exist")
            db.execute(
                """
                INSERT INTO kb_published_documents VALUES (?, ?, ?)
                ON CONFLICT (knowledge_base_id, document_id) DO UPDATE
                    SET revision_id = excluded.revision_id
            """,
                (*key, revision_id),
            )

    def unpublish_document(
        self, knowledge_base_id: str, document_id: str, *, expected_revision: str
    ) -> None:
        """只移除匹配的发布指针，并保留该文档各版本。"""
        with self._transaction(write=True) as db:
            cursor = db.execute(
                "DELETE FROM kb_published_documents "
                "WHERE knowledge_base_id = ? AND document_id = ? AND revision_id = ?",
                (knowledge_base_id, document_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflictError("published revision no longer matches")

    def read_published(
        self, knowledge_base_id: str, *, document_ids: Sequence[str]
    ) -> tuple[KnowledgeRevision, ...]:
        """在同一读事务内获取指定文档及其发布版本。

        分批绑定参数控制 SQLite 语句大小。所有批次共享一次读取快照。
        """
        ids = _document_scope(document_ids)
        if not ids:
            return ()
        payloads: dict[str, str] = {}
        with self._transaction() as db:
            for batch in batched(ids, 500):
                placeholders = ", ".join("?" for _ in batch)
                rows = db.execute(
                    f"""
                    SELECT p.document_id, r.payload
                    FROM kb_published_documents AS p
                    JOIN kb_document_revisions AS r
                        USING (knowledge_base_id, document_id, revision_id)
                    WHERE p.knowledge_base_id = ? AND p.document_id IN ({placeholders})
                """,
                    (knowledge_base_id, *batch),
                )
                payloads.update(rows)
        return tuple(
            KnowledgeRevision.model_validate_json(payloads[key]) for key in ids if key in payloads
        )
