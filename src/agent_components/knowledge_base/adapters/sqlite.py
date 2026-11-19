"""以 SQLite 持久化知识库内容与发布指针。"""

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import replace
from hashlib import sha256
from itertools import batched
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from agent_components.core.errors import RevisionConflictError
from agent_components.knowledge_base.artifacts import ArtifactRef
from agent_components.knowledge_base.contracts import KnowledgeRevision
from agent_components.knowledge_base.lifecycle import (
    Catalog,
    DocumentStatus,
    IndexJob,
    IndexJobStatus,
    RevisionArtifacts,
)
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


def _validated_payload(revision: KnowledgeRevision) -> str:
    # JSON 可能把 NaN 或无穷大变成 null。保存前先验证读回的快照。
    try:
        payload = _encode_revision(revision)
        restored = KnowledgeRevision.model_validate_json(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("knowledge revision must round-trip through JSON") from exc
    if _encode_revision(restored) != payload:
        raise ValueError("knowledge revision must round-trip through JSON without loss")
    return payload


def _validate_page(offset: int, limit: int) -> None:
    if (
        type(offset) is not int
        or not 0 <= offset < 2**63
        or type(limit) is not int
        or not 0 <= limit < 2**63
    ):
        raise ValueError("offset and limit must be nonnegative SQLite integers")


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
            db.execute("""
                CREATE TABLE IF NOT EXISTS kb_catalogs (
                    id TEXT PRIMARY KEY NOT NULL,
                    name TEXT NOT NULL,
                    index_id TEXT UNIQUE NOT NULL,
                    embedding_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    generation TEXT UNIQUE NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS kb_retired_catalog_versions (
                    id TEXT PRIMARY KEY NOT NULL,
                    version INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS kb_revision_artifacts (
                    knowledge_base_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    PRIMARY KEY (knowledge_base_id, document_id, revision_id, kind),
                    FOREIGN KEY (knowledge_base_id, document_id, revision_id)
                        REFERENCES kb_document_revisions
                            (knowledge_base_id, document_id, revision_id) ON DELETE CASCADE
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS kb_artifact_identity
                    ON kb_revision_artifacts (kind, sha256)
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS kb_index_jobs (
                    id TEXT PRIMARY KEY NOT NULL,
                    knowledge_base_id TEXT NOT NULL REFERENCES kb_catalogs (id),
                    document_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
                    attempts INTEGER NOT NULL,
                    error TEXT,
                    UNIQUE (knowledge_base_id, document_id, revision_id),
                    FOREIGN KEY (knowledge_base_id, document_id, revision_id)
                        REFERENCES kb_document_revisions
                            (knowledge_base_id, document_id, revision_id) ON DELETE CASCADE
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS kb_deleted_documents (
                    knowledge_base_id TEXT NOT NULL REFERENCES kb_catalogs (id) ON DELETE CASCADE,
                    document_id TEXT NOT NULL,
                    previous_revision TEXT,
                    PRIMARY KEY (knowledge_base_id, document_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS kb_pending_artifact_cleanup (
                    knowledge_base_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    PRIMARY KEY (knowledge_base_id, document_id, kind, sha256),
                    FOREIGN KEY (knowledge_base_id, document_id)
                        REFERENCES kb_deleted_documents (knowledge_base_id, document_id)
                )
            """)

    @property
    def workspace_identity(self) -> str:
        """获取内容数据库的规范文件 URI。"""
        return self._path.as_uri()

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
        payload = _validated_payload(revision)
        key = (knowledge_base_id, revision.document.id, revision.document.revision_id)
        with self._transaction(write=True) as db:
            self._require_unmanaged(db, knowledge_base_id)
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
            self._require_unmanaged(db, knowledge_base_id)
            self._check_publication(db, *key, expected_revision)
            if self._revision_row(db, *key, revision_id) is None:
                raise LookupError("knowledge revision does not exist")
            self._set_publication(db, *key, revision_id)

    def unpublish_document(
        self,
        knowledge_base_id: str,
        document_id: str,
        *,
        expected_revision: str,
        expected_generation: str | None = None,
    ) -> None:
        """只移除匹配的发布指针，并保留该文档各版本。"""
        with self._transaction(write=True) as db:
            if expected_generation is not None:
                self._require_catalog(db, knowledge_base_id, expected_generation)
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

    @staticmethod
    def _catalog_row(db: sqlite3.Connection, kb_id: str) -> Catalog | None:
        row = db.execute(
            "SELECT id, name, index_id, embedding_id, version, generation "
            "FROM kb_catalogs WHERE id = ?",
            (kb_id,),
        ).fetchone()
        return None if row is None else Catalog(*row)

    @classmethod
    def _require_catalog(
        cls, db: sqlite3.Connection, kb_id: str, expected_generation: str | None = None
    ) -> Catalog:
        catalog = cls._catalog_row(db, kb_id)
        if catalog is None:
            raise LookupError("knowledge base catalog does not exist")
        if expected_generation is not None and catalog.generation != expected_generation:
            raise RevisionConflictError("catalog generation no longer matches")
        return catalog

    @classmethod
    def _require_unmanaged(cls, db: sqlite3.Connection, kb_id: str) -> None:
        if cls._catalog_row(db, kb_id) is not None:
            raise RevisionConflictError("managed knowledge bases require lifecycle operations")

    @staticmethod
    def _publication(db: sqlite3.Connection, kb_id: str, doc_id: str) -> str | None:
        row = db.execute(
            "SELECT revision_id FROM kb_published_documents "
            "WHERE knowledge_base_id = ? AND document_id = ?",
            (kb_id, doc_id),
        ).fetchone()
        return None if row is None else str(row[0])

    @classmethod
    def _check_publication(
        cls, db: sqlite3.Connection, kb_id: str, doc_id: str, expected_revision: str | None
    ) -> None:
        if cls._publication(db, kb_id, doc_id) != expected_revision:
            raise RevisionConflictError("published revision no longer matches")

    @staticmethod
    def _set_publication(db: sqlite3.Connection, kb_id: str, doc_id: str, rev_id: str) -> None:
        db.execute(
            """
            INSERT INTO kb_published_documents VALUES (?, ?, ?)
            ON CONFLICT (knowledge_base_id, document_id) DO UPDATE
                SET revision_id = excluded.revision_id
            """,
            (kb_id, doc_id, rev_id),
        )

    @staticmethod
    def _tombstone(db: sqlite3.Connection, kb_id: str, doc_id: str) -> tuple[str | None] | None:
        row = db.execute(
            "SELECT previous_revision FROM kb_deleted_documents "
            "WHERE knowledge_base_id = ? AND document_id = ?",
            (kb_id, doc_id),
        ).fetchone()
        return None if row is None else (row[0],)

    @classmethod
    def _require_live(cls, db: sqlite3.Connection, kb_id: str, doc_id: str) -> None:
        if cls._tombstone(db, kb_id, doc_id) is not None:
            raise RevisionConflictError("document is deleted")

    def create_catalog(self, catalog: Catalog) -> None:
        """创建目录并保留索引绑定的独占性。"""
        values = (catalog.id, catalog.name, catalog.index_id, catalog.embedding_id)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("catalog identity, name and bindings must be nonblank strings")
        if type(catalog.version) is not int or not 0 < catalog.version < 2**63:
            raise ValueError("catalog version must be a positive SQLite integer")
        if catalog.generation is not None:
            raise ValueError("catalog generation is assigned by the repository")
        with self._transaction(write=True) as db:
            contents = db.execute(
                "SELECT 1 FROM kb_document_revisions WHERE knowledge_base_id = ? LIMIT 1",
                (catalog.id,),
            ).fetchone()
            if contents is not None:
                raise RevisionConflictError("catalog ID already has stored revisions")
            retired = db.execute(
                "SELECT version FROM kb_retired_catalog_versions WHERE id = ?",
                (catalog.id,),
            ).fetchone()
            version = catalog.version if retired is None else max(catalog.version, retired[0] + 1)
            if version >= 2**63:
                raise RevisionConflictError("catalog version cannot be advanced")
            try:
                db.execute(
                    "INSERT INTO kb_catalogs VALUES (?, ?, ?, ?, ?, ?)",
                    (*values, version, uuid4().hex),
                )
            except sqlite3.IntegrityError as exc:
                raise RevisionConflictError("catalog ID or index binding already exists") from exc

    def get_catalog(self, kb_id: str) -> Catalog | None:
        """获取一个目录及其当前版本。"""
        with self._transaction() as db:
            return self._catalog_row(db, kb_id)

    def list_catalogs(self) -> tuple[Catalog, ...]:
        """按标识符顺序列出目录。"""
        with self._transaction() as db:
            rows = db.execute(
                "SELECT id, name, index_id, embedding_id, version, generation "
                "FROM kb_catalogs ORDER BY id"
            )
            return tuple(Catalog(*row) for row in rows)

    def update_catalog(self, kb_id: str, name: str, *, expected_version: int) -> Catalog:
        """在校验目录版本后更新名称。"""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("catalog name must be a nonblank string")
        with self._transaction(write=True) as db:
            catalog = self._require_catalog(db, kb_id)
            if catalog.version != expected_version:
                raise RevisionConflictError("catalog version no longer matches")
            if catalog.version == 2**63 - 1:
                raise RevisionConflictError("catalog version cannot be advanced")
            updated = replace(catalog, name=name, version=catalog.version + 1)
            db.execute(
                "UPDATE kb_catalogs SET name = ?, version = ? WHERE id = ?",
                (updated.name, updated.version, kb_id),
            )
            return updated

    def delete_catalog(self, kb_id: str, *, expected_version: int) -> None:
        """删除内容及相关产物已清理完毕的目录。"""
        with self._transaction(write=True) as db:
            catalog = self._require_catalog(db, kb_id)
            if catalog.version != expected_version:
                raise RevisionConflictError("catalog version no longer matches")
            contents = db.execute(
                """
                SELECT 1 FROM kb_document_revisions WHERE knowledge_base_id = ?
                UNION ALL
                SELECT 1 FROM kb_pending_artifact_cleanup WHERE knowledge_base_id = ?
                LIMIT 1
                """,
                (kb_id, kb_id),
            ).fetchone()
            if contents is not None:
                raise RevisionConflictError("catalog contents must be deleted and purged first")
            db.execute(
                """
                INSERT INTO kb_retired_catalog_versions VALUES (?, ?)
                ON CONFLICT (id) DO UPDATE SET version = excluded.version
                """,
                (kb_id, catalog.version),
            )
            db.execute("DELETE FROM kb_catalogs WHERE id = ?", (kb_id,))

    def save_managed_revision(
        self,
        kb_id: str,
        revision: KnowledgeRevision,
        create_artifacts: Callable[[], RevisionArtifacts],
        *,
        expected_generation: str | None = None,
    ) -> None:
        """为新版本保存不可变的内容和产物引用。"""
        payload = _validated_payload(revision)
        key = (kb_id, revision.document.id, revision.document.revision_id)
        with self._transaction(write=True) as db:
            self._require_catalog(db, kb_id, expected_generation)
            self._require_live(db, kb_id, revision.document.id)
            existing = self._revision_row(db, *key)
            if existing is not None:
                if existing != payload:
                    raise RevisionConflictError("knowledge revision already has different content")
                return
            refs = create_artifacts()
            references = (refs.source, refs.parsed, refs.markdown)
            if tuple(ref.kind for ref in references) != ("source", "parsed", "markdown"):
                raise ValueError("revision artifacts must match their assigned kinds")
            db.execute("INSERT INTO kb_document_revisions VALUES (?, ?, ?, ?)", (*key, payload))
            db.executemany(
                "INSERT INTO kb_revision_artifacts VALUES (?, ?, ?, ?, ?, ?)",
                ((*key, ref.kind, ref.sha256, ref.size) for ref in references),
            )

    def get_artifacts(self, kb_id: str, doc_id: str, rev_id: str) -> RevisionArtifacts | None:
        """恢复指定版本的产物引用。"""
        with self._transaction() as db:
            rows = db.execute(
                "SELECT kind, sha256, size FROM kb_revision_artifacts "
                "WHERE knowledge_base_id = ? AND document_id = ? AND revision_id = ?",
                (kb_id, doc_id, rev_id),
            )
            refs = {
                kind: ArtifactRef(kind=kind, sha256=digest, size=size)
                for kind, digest, size in rows
            }
        if not refs:
            return None
        return RevisionArtifacts(refs["source"], refs["parsed"], refs["markdown"])

    def list_documents(self, kb_id: str, *, offset: int, limit: int) -> tuple[DocumentStatus, ...]:
        """按文档标识符分页列出内容和保留的删除标记。"""
        _validate_page(offset, limit)
        with self._transaction() as db:
            self._require_catalog(db, kb_id)
            rows = db.execute(
                """
                WITH documents AS (
                    SELECT document_id FROM kb_document_revisions WHERE knowledge_base_id = :kb
                    UNION
                    SELECT document_id FROM kb_deleted_documents WHERE knowledge_base_id = :kb
                )
                SELECT d.document_id, p.revision_id, t.document_id IS NOT NULL
                FROM documents AS d
                LEFT JOIN kb_published_documents AS p
                    ON p.knowledge_base_id = :kb AND p.document_id = d.document_id
                LEFT JOIN kb_deleted_documents AS t
                    ON t.knowledge_base_id = :kb AND t.document_id = d.document_id
                ORDER BY d.document_id LIMIT :limit OFFSET :offset
                """,
                {"kb": kb_id, "limit": limit, "offset": offset},
            )
            return tuple(
                DocumentStatus(doc_id, pub_revision, bool(deleted))
                for doc_id, pub_revision, deleted in rows
            )

    def list_revisions(
        self, kb_id: str, doc_id: str, *, offset: int, limit: int
    ) -> tuple[str, ...]:
        """按版本标识符分页列出文档的持久化版本。"""
        _validate_page(offset, limit)
        with self._transaction() as db:
            self._require_catalog(db, kb_id)
            rows = db.execute(
                "SELECT revision_id FROM kb_document_revisions "
                "WHERE knowledge_base_id = ? AND document_id = ? "
                "ORDER BY revision_id LIMIT ? OFFSET ?",
                (kb_id, doc_id, limit, offset),
            )
            return tuple(str(row[0]) for row in rows)

    @staticmethod
    def _job_for_revision(
        db: sqlite3.Connection, kb_id: str, doc_id: str, rev_id: str
    ) -> IndexJob | None:
        row = db.execute(
            "SELECT id, knowledge_base_id, document_id, revision_id, status, attempts, error "
            "FROM kb_index_jobs "
            "WHERE knowledge_base_id = ? AND document_id = ? AND revision_id = ?",
            (kb_id, doc_id, rev_id),
        ).fetchone()
        return None if row is None else IndexJob(*row)

    @staticmethod
    def _job_row(db: sqlite3.Connection, kb_id: str, job_id: str) -> IndexJob | None:
        row = db.execute(
            "SELECT id, knowledge_base_id, document_id, revision_id, status, attempts, error "
            "FROM kb_index_jobs WHERE knowledge_base_id = ? AND id = ?",
            (kb_id, job_id),
        ).fetchone()
        return None if row is None else IndexJob(*row)

    @classmethod
    def _require_live_job(cls, db: sqlite3.Connection, kb_id: str, job_id: str) -> IndexJob:
        job = cls._job_row(db, kb_id, job_id)
        if job is None:
            raise LookupError("index job does not exist")
        cls._require_live(db, kb_id, job.document_id)
        return job

    @staticmethod
    def _set_job(
        db: sqlite3.Connection, job: IndexJob, status: IndexJobStatus, attempts: int
    ) -> IndexJob:
        db.execute(
            "UPDATE kb_index_jobs SET status = ?, attempts = ?, error = NULL WHERE id = ?",
            (status, attempts, job.id),
        )
        return replace(job, status=status, attempts=attempts, error=None)

    def enqueue_index(
        self, kb_id: str, doc_id: str, rev_id: str, *, expected_generation: str | None = None
    ) -> IndexJob:
        """为版本创建或恢复同一个索引任务。"""
        with self._transaction(write=True) as db:
            catalog = self._require_catalog(db, kb_id, expected_generation)
            self._require_live(db, kb_id, doc_id)
            job = self._job_for_revision(db, kb_id, doc_id, rev_id)
            if job is not None:
                return job
            if self._revision_row(db, kb_id, doc_id, rev_id) is None:
                raise LookupError("knowledge revision does not exist")
            identity = json.dumps((kb_id, catalog.index_id, catalog.generation, doc_id, rev_id))
            job_id = sha256(identity.encode()).hexdigest()
            db.execute(
                "INSERT INTO kb_index_jobs VALUES (?, ?, ?, ?, 'queued', 0, NULL)",
                (job_id, kb_id, doc_id, rev_id),
            )
            return IndexJob(job_id, kb_id, doc_id, rev_id, "queued", 0, None)

    def get_index_job(self, kb_id: str, job_id: str) -> IndexJob | None:
        """获取指定目录的任务状态。"""
        with self._transaction() as db:
            return self._job_row(db, kb_id, job_id)

    def list_index_jobs(
        self, kb_id: str, *, status: IndexJobStatus | None, offset: int, limit: int
    ) -> tuple[IndexJob, ...]:
        """按稳定顺序分页列出任务，可按状态过滤。"""
        _validate_page(offset, limit)
        if status not in (None, "queued", "running", "succeeded", "failed"):
            raise ValueError("unknown index job status")
        query = (
            "SELECT id, knowledge_base_id, document_id, revision_id, status, attempts, error "
            "FROM kb_index_jobs WHERE knowledge_base_id = ?"
        )
        params: tuple[str, ...] = (kb_id,)
        if status is not None:
            query += " AND status = ?"
            params += (status,)
        with self._transaction() as db:
            self._require_catalog(db, kb_id)
            rows = db.execute(
                query + " ORDER BY document_id, revision_id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            return tuple(IndexJob(*row) for row in rows)

    def claim_index(self, kb_id: str, job_id: str) -> IndexJob:
        """领取任务并为此次执行分配尝试编号。"""
        with self._transaction(write=True) as db:
            job = self._require_live_job(db, kb_id, job_id)
            if job.status == "succeeded":
                return job
            if job.status == "running":
                raise RevisionConflictError("index job is already running")
            return self._set_job(db, job, "running", job.attempts + 1)

    def finish_index(
        self, kb_id: str, job_id: str, attempt: int, write_index: Callable[[], None]
    ) -> IndexJob:
        """在写锁内检查尝试编号、写入向量并记录成功。"""
        with self._transaction(write=True) as db:
            job = self._require_live_job(db, kb_id, job_id)
            if job.attempts != attempt or job.status not in ("running", "succeeded"):
                raise RevisionConflictError("index attempt is no longer active")
            if job.status == "succeeded":
                return job
            write_index()
            return self._set_job(db, job, "succeeded", attempt)

    def fail_index(self, kb_id: str, job_id: str, attempt: int, error: str) -> None:
        """记录失败，保留已经重置、完成或删除的任务状态。"""
        with self._transaction(write=True) as db:
            db.execute(
                """
                UPDATE kb_index_jobs AS job SET status = 'failed', error = ?
                WHERE knowledge_base_id = ? AND id = ? AND attempts = ? AND status = 'running'
                    AND NOT EXISTS (
                        SELECT 1 FROM kb_deleted_documents AS deleted
                        WHERE deleted.knowledge_base_id = job.knowledge_base_id
                            AND deleted.document_id = job.document_id
                    )
                """,
                (error, kb_id, job_id, attempt),
            )

    def reset_index(self, kb_id: str, job_id: str, *, expected_attempt: int) -> IndexJob:
        """重置当前任务，重建前须先取消发布目标版本。"""
        with self._transaction(write=True) as db:
            job = self._require_live_job(db, kb_id, job_id)
            if job.attempts != expected_attempt:
                raise RevisionConflictError("index attempt no longer matches")
            if self._publication(db, kb_id, job.document_id) == job.revision_id:
                raise RevisionConflictError("unpublish the revision before resetting its index")
            return self._set_job(db, job, "queued", job.attempts)

    def publish_ready(
        self,
        kb_id: str,
        doc_id: str,
        rev_id: str,
        *,
        expected_revision: str | None,
        verify_index: Callable[[], None],
        expected_generation: str | None = None,
    ) -> None:
        """发布已成功写入且现场核验过索引的版本。"""
        with self._transaction(write=True) as db:
            self._require_catalog(db, kb_id, expected_generation)
            self._require_live(db, kb_id, doc_id)
            self._check_publication(db, kb_id, doc_id, expected_revision)
            if self._revision_row(db, kb_id, doc_id, rev_id) is None:
                raise LookupError("knowledge revision does not exist")
            job = self._job_for_revision(db, kb_id, doc_id, rev_id)
            if job is None or job.status != "succeeded":
                raise RevisionConflictError("knowledge revision index is not ready")
            verify_index()
            self._set_publication(db, kb_id, doc_id, rev_id)

    def mark_deleted(
        self,
        kb_id: str,
        doc_id: str,
        *,
        expected_revision: str | None,
        expected_generation: str | None = None,
    ) -> None:
        """在同一事务中保存删除标记并移除发布指针。"""
        with self._transaction(write=True) as db:
            self._require_catalog(db, kb_id, expected_generation)
            tombstone = self._tombstone(db, kb_id, doc_id)
            if tombstone is not None:
                if expected_revision not in (None, tombstone[0]):
                    raise RevisionConflictError("published revision no longer matches")
                return
            self._check_publication(db, kb_id, doc_id, expected_revision)
            exists = db.execute(
                "SELECT 1 FROM kb_document_revisions "
                "WHERE knowledge_base_id = ? AND document_id = ? LIMIT 1",
                (kb_id, doc_id),
            ).fetchone()
            if exists is None:
                raise LookupError("knowledge document does not exist")
            db.execute(
                "INSERT INTO kb_deleted_documents VALUES (?, ?, ?)",
                (kb_id, doc_id, expected_revision),
            )
            db.execute(
                "DELETE FROM kb_published_documents WHERE knowledge_base_id = ? "
                "AND document_id = ?",
                (kb_id, doc_id),
            )

    def purge_deleted(
        self,
        kb_id: str,
        doc_id: str,
        cleanup_vectors: Callable[[Sequence[str]], None],
        cleanup_artifact: Callable[[ArtifactRef], None],
        *,
        expected_generation: str | None = None,
    ) -> None:
        """可重试地清除文档的向量、版本和失去引用的文件。"""
        key = (kb_id, doc_id)
        with self._transaction(write=True) as db:
            catalog = self._require_catalog(db, kb_id, expected_generation)
            if self._tombstone(db, *key) is None:
                raise RevisionConflictError("mark the document deleted before purging")
            rows = db.execute(
                "SELECT payload FROM kb_document_revisions "
                "WHERE knowledge_base_id = ? AND document_id = ? ORDER BY revision_id",
                key,
            )
            leaf_ids = dict.fromkeys(
                chunk.id
                for (payload,) in rows
                for chunk in KnowledgeRevision.model_validate_json(payload).chunks.leaves
            )
            if leaf_ids:
                cleanup_vectors(tuple(leaf_ids))
            # 先持久化待清理文件，再移除版本引用。文件删除失败可重试，
            # 中间生成的备份也不会引用已经删除的文件。
            db.execute(
                """
                INSERT OR IGNORE INTO kb_pending_artifact_cleanup
                SELECT knowledge_base_id, document_id, kind, sha256, size
                FROM kb_revision_artifacts WHERE knowledge_base_id = ? AND document_id = ?
                """,
                key,
            )
            db.execute(
                "DELETE FROM kb_document_revisions WHERE knowledge_base_id = ? AND document_id = ?",
                key,
            )

        with self._transaction(write=True) as db:
            entry = self._catalog_row(db, kb_id)
            if entry is None or entry.generation != catalog.generation:
                return
            rows = db.execute(
                """
                SELECT pending.kind, pending.sha256, pending.size
                FROM kb_pending_artifact_cleanup AS pending
                WHERE pending.knowledge_base_id = ? AND pending.document_id = ?
                    AND NOT EXISTS (
                        SELECT 1 FROM kb_revision_artifacts AS live
                        WHERE live.kind = pending.kind AND live.sha256 = pending.sha256
                    )
                ORDER BY pending.kind, pending.sha256
                """,
                key,
            )
            for kind, digest, size in rows:
                cleanup_artifact(ArtifactRef(kind=kind, sha256=digest, size=size))
            db.execute(
                "DELETE FROM kb_pending_artifact_cleanup "
                "WHERE knowledge_base_id = ? AND document_id = ?",
                key,
            )

    def collect_orphan_artifacts(
        self,
        *,
        list_artifacts: Callable[[], Sequence[ArtifactRef]],
        delete_artifact: Callable[[ArtifactRef], None],
    ) -> int:
        """在写锁内删除工作区所有目录均未引用的文件。"""
        with self._transaction(write=True) as db:
            referenced = set(db.execute("SELECT DISTINCT kind, sha256 FROM kb_revision_artifacts"))
            removed = 0
            for ref in list_artifacts():
                identity = (ref.kind, ref.sha256)
                if identity not in referenced:
                    delete_artifact(ref)
                    referenced.add(identity)
                    removed += 1
            return removed

    def backup_snapshot(self, write_bundle: Callable[[Path, Sequence[ArtifactRef]], None]) -> None:
        """在内容及其文件引用保持稳定时生成 SQLite 快照。"""
        with TemporaryDirectory(prefix="knowledge-backup-") as directory:
            snapshot = Path(directory) / "knowledge.sqlite"
            with self._transaction(write=True) as db:
                rows = db.execute(
                    "SELECT DISTINCT kind, sha256, size FROM kb_revision_artifacts "
                    "ORDER BY kind, sha256, size"
                )
                refs = tuple(
                    ArtifactRef(kind=kind, sha256=digest, size=size) for kind, digest, size in rows
                )
                # 由独立连接读取，持有写事务的连接不能作为备份源。
                with closing(sqlite3.connect(self._path)) as source:
                    with closing(sqlite3.connect(snapshot)) as destination:
                        source.backup(destination)
                        destination.execute("PRAGMA journal_mode = DELETE").fetchone()
                write_bundle(snapshot, refs)

    def invalidate_restored_indexes(self) -> None:
        """清除恢复库的发布指针并重新排队已有索引任务。"""
        with self._transaction(write=True) as db:
            db.execute("DELETE FROM kb_published_documents")
            db.execute("UPDATE kb_index_jobs SET status = 'queued', error = NULL")
