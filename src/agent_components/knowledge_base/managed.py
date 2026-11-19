"""组合可替换的文件、内容、向量和检索端口，提供完整的知识库生命周期。"""

import json
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from itertools import batched
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from agent_components.chunking import (
    Chunker,
    ChunkPolicy,
    EmbeddingProvider,
    RechunkedDocument,
    Rechunker,
    SectionOptions,
)
from agent_components.core.contracts import Chunk, ChunkResult, Document, ParsedDocument
from agent_components.core.errors import RevisionConflictError, UnsupportedCapabilityError
from agent_components.knowledge_base.artifacts import (
    ArtifactKind,
    ArtifactRef,
    ArtifactStore,
    EnumerableArtifactStore,
    WorkspaceArtifactStore,
)
from agent_components.knowledge_base.backup import restore_backup, write_backup
from agent_components.knowledge_base.contracts import KnowledgeRevision
from agent_components.knowledge_base.lifecycle import (
    Catalog,
    DocumentStatus,
    IndexJob,
    IndexJobStatus,
    LifecycleRepository,
    RevisionArtifacts,
)
from agent_components.knowledge_base.repository import _document_scope
from agent_components.knowledge_base.service import KnowledgeBase
from agent_components.retrieval.pipeline import RetrievalOptions, RetrievalPipeline, RetrievalResult
from agent_components.retrieval.reranking import RerankerProvider
from agent_components.retrieval.vector_store import IndexRecord, KeywordStore, ManagedVectorStore


class ManagedKnowledgeBase(KnowledgeBase):
    """管理单个知识库的解析产物、索引状态和有授权范围的检索。

    应用负责绑定独占的物理索引并选定 Embedding Provider。上传鉴权、审批和任务调度由
    调用方控制。此组件持久化任务状态并提供可重试的执行入口。
    """

    def __init__(
        self,
        knowledge_base_id: str,
        repository: LifecycleRepository,
        artifacts: ArtifactStore,
        index: ManagedVectorStore,
        embedding: EmbeddingProvider,
        *,
        index_id: str,
        embedding_id: str,
        reranker: RerankerProvider | None = None,
        keyword_index: KeywordStore | None = None,
        chunker: Chunker | None = None,
    ) -> None:
        """检查配置绑定，注入文件存储、索引、分块和模型提供方。"""
        super().__init__(knowledge_base_id, repository)
        self._lifecycle = repository
        self._artifacts = artifacts
        self._index = index
        self._embedding = embedding
        self._binding = (index_id, embedding_id)
        self._generation: str | None = None
        self._generation = self._check_binding().generation
        if isinstance(artifacts, WorkspaceArtifactStore):
            artifacts.bind_workspace(repository.workspace_identity)
        self._rechunker = Rechunker(chunker)
        self._retrieval = RetrievalPipeline(
            index, embedding, reranker=reranker, keyword_index=keyword_index
        )

    def _check_binding(self) -> Catalog:
        catalog = self._lifecycle.get_catalog(self._id)
        if catalog is None:
            raise LookupError("knowledge base catalog does not exist")
        if (catalog.index_id, catalog.embedding_id) != self._binding:
            raise RevisionConflictError("knowledge base index or embedding binding does not match")
        if self._generation is not None and self._generation != catalog.generation:
            raise RevisionConflictError("knowledge base catalog generation does not match")
        return catalog

    def save_revision(self, document: Document, chunks: ChunkResult) -> None:
        """托管版本需同时保存源文件和解析产物，统一走 save_parsed。"""
        raise RevisionConflictError("use save_parsed to create a managed knowledge revision")

    def save_parsed(
        self,
        parsed: ParsedDocument,
        *,
        source: bytes,
        policy: ChunkPolicy,
        section_options: SectionOptions | None = None,
    ) -> RechunkedDocument:
        """校验原始文件及解析产物归属，保存三个产物并生成草稿版本。"""
        return self._save(parsed, source, policy, section_options, previous_revision_id=None)

    def _save(
        self,
        parsed: ParsedDocument,
        source: bytes,
        policy: ChunkPolicy,
        section_options: SectionOptions | None,
        *,
        previous_revision_id: str | None,
    ) -> RechunkedDocument:
        self._check_binding()
        if not isinstance(source, bytes):
            raise TypeError("source must be bytes")
        # 切分输入与落盘 JSON 使用同一规范化快照，避免调用方修改原对象的嵌套内容。
        payload = json.dumps(
            parsed.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        parsed = ParsedDocument.model_validate_json(payload)
        source_digest = sha256(source).hexdigest()
        expected_digest = parsed.provenance.source_sha256 if parsed.provenance else None
        if source_digest != expected_digest and (
            expected_digest is not None or source_digest != parsed.document.revision_id
        ):
            raise ValueError("source bytes do not match the parsed document SHA-256")
        parsed_digest = sha256(payload).hexdigest()
        result = self._rechunker.rechunk(
            parsed,
            policy,
            section_options=section_options,
            artifact_ref=f"sha256:{parsed_digest}",
            previous_revision_id=previous_revision_id,
        )

        def persist() -> RevisionArtifacts:
            return RevisionArtifacts(
                source=self._put("source", source, digest=source_digest),
                parsed=self._put("parsed", payload, digest=parsed_digest),
                markdown=self._put("markdown", parsed.markdown.encode("utf-8")),
            )

        self._lifecycle.save_managed_revision(
            self._id,
            KnowledgeRevision(document=result.document, chunks=result.chunks),
            persist,
            expected_generation=self._generation,
        )
        return result

    def _put(self, kind: ArtifactKind, data: bytes, *, digest: str | None = None) -> ArtifactRef:
        ref = self._artifacts.put(kind, data)
        expected = (kind, digest or sha256(data).hexdigest(), len(data))
        if (ref.kind, ref.sha256, ref.size) != expected:
            raise ValueError("artifact store returned an invalid reference")
        return ref

    def _artifact_refs(self, document_id: str, revision_id: str) -> RevisionArtifacts:
        refs = self._lifecycle.get_artifacts(self._id, document_id, revision_id)
        if refs is None:
            raise LookupError("knowledge revision artifacts do not exist")
        return refs

    def read_artifact(self, document_id: str, revision_id: str, kind: ArtifactKind) -> bytes:
        """返回指定版本的原始字节、完整解析 JSON 或 UTF-8 Markdown。"""
        self._check_binding()
        refs = self._artifact_refs(document_id, revision_id)
        return self._artifacts.read(
            {"source": refs.source, "parsed": refs.parsed, "markdown": refs.markdown}[kind]
        )

    def rechunk_revision(
        self,
        document_id: str,
        revision_id: str,
        policy: ChunkPolicy,
        *,
        section_options: SectionOptions | None = None,
    ) -> RechunkedDocument:
        """读取历史版本产物并派生草稿，无需重新运行 Docling。"""
        self._check_binding()
        refs = self._artifact_refs(document_id, revision_id)
        parsed = ParsedDocument.model_validate_json(self._artifacts.read(refs.parsed))
        if parsed.document.id != document_id:
            raise RevisionConflictError("parsed artifact belongs to a different document")
        return self._save(
            parsed,
            self._artifacts.read(refs.source),
            policy,
            section_options,
            previous_revision_id=revision_id,
        )

    def list_documents(self, *, offset: int = 0, limit: int = 100) -> tuple[DocumentStatus, ...]:
        """按文档 ID 分页返回发布和删除状态。"""
        self._check_binding()
        return self._lifecycle.list_documents(self._id, offset=offset, limit=limit)

    def list_revisions(
        self, document_id: str, *, offset: int = 0, limit: int = 100
    ) -> tuple[str, ...]:
        """按版本 ID 分页列出持久化版本。"""
        self._check_binding()
        return self._lifecycle.list_revisions(self._id, document_id, offset=offset, limit=limit)

    def get_revision(self, document_id: str, revision_id: str) -> KnowledgeRevision | None:
        """供已授权的管理端读取指定草稿或历史版本。"""
        self._check_binding()
        return super().get_revision(document_id, revision_id)

    def _require_revision(self, document_id: str, revision_id: str) -> KnowledgeRevision:
        revision = self.get_revision(document_id, revision_id)
        if revision is None:
            raise LookupError("knowledge revision does not exist")
        return revision

    def chunk_page(
        self,
        document_id: str,
        revision_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
        kind: Literal["leaf", "parent"] | None = None,
    ) -> tuple[Chunk, ...]:
        """按文档顺序分页读取分块，适用于预览和调试。"""
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ValueError("offset must be nonnegative and limit must be between 1 and 1000")
        if kind not in (None, "leaf", "parent"):
            raise ValueError("invalid chunk kind")
        chunks = self._require_revision(document_id, revision_id).chunks.chunks
        if kind is not None:
            chunks = tuple(chunk for chunk in chunks if chunk.kind == kind)
        return chunks[offset : offset + limit]

    def enqueue_index(self, document_id: str, revision_id: str) -> IndexJob:
        """为版本获取一个持久、可重复提交的索引任务。"""
        self._check_binding()
        return self._lifecycle.enqueue_index(
            self._id, document_id, revision_id, expected_generation=self._generation
        )

    def get_index_job(self, job_id: str) -> IndexJob | None:
        """查询任务状态和尝试编号。"""
        self._check_binding()
        return self._lifecycle.get_index_job(self._id, job_id)

    def list_index_jobs(
        self, *, status: IndexJobStatus | None = None, offset: int = 0, limit: int = 100
    ) -> tuple[IndexJob, ...]:
        """分页发现索引任务，可按任务状态过滤。"""
        self._check_binding()
        return self._lifecycle.list_index_jobs(self._id, status=status, offset=offset, limit=limit)

    def reset_index(self, job_id: str, *, expected_attempt: int) -> IndexJob:
        """重新排队。在重置已发布版本前先取消发布。"""
        self._check_binding()
        return self._lifecycle.reset_index(self._id, job_id, expected_attempt=expected_attempt)

    def _valid_vector(self, vector: Sequence[float]) -> bool:
        norm = math.hypot(*vector)
        return len(vector) == self._index.dimensions and math.isfinite(norm) and norm > 0

    def _verify_index(self, leaves: Sequence[Chunk]) -> None:
        # 发布依赖真实可读的叶子行。仅有任务成功标记不足以证明索引仍存在。
        for group in batched(leaves, 512):
            expected = {chunk.id: chunk for chunk in group}
            records = self._index.get_records(chunk_ids=tuple(expected))
            found = {record.id for record in records}
            if len(records) != len(expected) or found != expected.keys():
                raise RevisionConflictError("index is missing expected leaf records")
            for record in records:
                chunk = expected[record.id]
                if (record.document_id, record.revision_id, record.text) != (
                    chunk.document_id,
                    chunk.revision_id,
                    chunk.index_text,
                ) or not self._valid_vector(record.vector):
                    raise RevisionConflictError("index record does not match the revision")

    def run_index(self, job_id: str, *, batch_size: int = 32) -> IndexJob:
        """领取并执行一个任务。批量计算向量后，为仍有效的尝试写入索引。"""
        if type(batch_size) is not int or not 1 <= batch_size <= 512:
            raise ValueError("batch_size must be between 1 and 512")
        self._check_binding()
        job = self._lifecycle.claim_index(self._id, job_id)
        if job.status == "succeeded":
            return job
        try:
            leaves = self._require_revision(job.document_id, job.revision_id).chunks.leaves
            records = []
            # 模型调用在数据库写事务之外，避免网络等待阻塞其他知识库操作。
            for group in batched(leaves, batch_size):
                vectors = self._embedding.embed_batch(tuple(chunk.index_text for chunk in group))
                if len(vectors) != len(group):
                    raise ValueError("embedding result count differs from input")
                for chunk, vector in zip(group, vectors, strict=True):
                    if not self._valid_vector(vector):
                        raise ValueError("embedding must be finite, nonzero and match dimension")
                    records.append(
                        IndexRecord(
                            id=chunk.id,
                            document_id=chunk.document_id,
                            revision_id=chunk.revision_id,
                            text=chunk.index_text,
                            vector=tuple(vector),
                        )
                    )

            def write_index() -> None:
                # 写入以稳定块 ID 为键。部分写入失败后的重试覆盖同一记录。
                for group in batched(records, batch_size):
                    self._index.upsert(group)
                self._verify_index(leaves)

            return self._lifecycle.finish_index(self._id, job.id, job.attempts, write_index)
        except Exception as exc:
            try:
                self._lifecycle.fail_index(self._id, job.id, job.attempts, "index_build_failed")
            except Exception:
                exc.add_note("Could not persist the index failure. Check the job before retrying.")
            raise

    def publish_revision(
        self, document_id: str, revision_id: str, *, expected_revision: str | None
    ) -> None:
        """在索引与当前发布版本都通过校验后切换发布指针。"""
        self._check_binding()
        revision = self._require_revision(document_id, revision_id)
        self._lifecycle.publish_ready(
            self._id,
            document_id,
            revision_id,
            expected_revision=expected_revision,
            verify_index=lambda: self._verify_index(revision.chunks.leaves),
            expected_generation=self._generation,
        )

    def unpublish_document(self, document_id: str, *, expected_revision: str) -> None:
        """按版本和目录绑定撤下文档；保留版本及其索引任务。"""
        self._check_binding()
        self._lifecycle.unpublish_document(
            self._id,
            document_id,
            expected_revision=expected_revision,
            expected_generation=self._generation,
        )

    def search(
        self,
        query: str,
        *,
        authorized_document_ids: Callable[[], Sequence[str]],
        document_ids: Sequence[str] | None = None,
        allowed_chunk: Callable[[Chunk], bool] | None = None,
        options: RetrievalOptions | None = None,
    ) -> RetrievalResult:
        """以当前发布版本和调用方实时授权限定检索，返回前再次核验可见块。"""
        requested = None if document_ids is None else set(_document_scope(document_ids))

        def visible_chunks() -> tuple[Chunk, ...]:
            self._check_binding()
            granted = _document_scope(authorized_document_ids())
            scope = tuple(doc_id for doc_id in granted if requested is None or doc_id in requested)
            chunks = self.published_chunks(document_ids=scope)
            if allowed_chunk is not None:
                allowed, hidden_parents = [], set()
                for chunk in chunks:
                    if allowed_chunk(chunk):
                        allowed.append(chunk)
                    elif chunk.parent_id is not None:
                        hidden_parents.add(chunk.parent_id)
                # 父块包含其子块原文。任意子块不可见时，父块也不能用于上下文。
                chunks = tuple(chunk for chunk in allowed if chunk.id not in hidden_parents)
            return tuple(sorted(chunks, key=lambda chunk: chunk.id))

        snapshot = visible_chunks()
        result = self._retrieval.search(
            query,
            visible_chunks=snapshot,
            options=options,
            allow_missing_parents=allowed_chunk is not None,
        )
        if visible_chunks() != snapshot:
            raise RevisionConflictError("document visibility changed during retrieval")
        return result

    def delete_document(self, document_id: str, *, expected_revision: str | None) -> None:
        """以版本条件撤下发布并标记删除，可检索范围立即收缩。"""
        self._check_binding()
        self._lifecycle.mark_deleted(
            self._id,
            document_id,
            expected_revision=expected_revision,
            expected_generation=self._generation,
        )

    def purge_deleted(self, document_id: str) -> None:
        """可重试地清理已删除文档及其索引与无人引用的产物。"""
        self._check_binding()

        def remove_vectors(ids: Sequence[str]) -> None:
            for group in batched(ids, 512):
                self._index.delete_records(chunk_ids=group)

        self._lifecycle.purge_deleted(
            self._id,
            document_id,
            remove_vectors,
            self._artifacts.delete,
            expected_generation=self._generation,
        )

    def collect_orphan_artifacts(self) -> int:
        """在整个工作区回收未被任何版本引用的文件，适用于中断上传后的清理。"""
        if not isinstance(self._artifacts, EnumerableArtifactStore):
            raise UnsupportedCapabilityError("artifact store does not support orphan collection")
        return self._lifecycle.collect_orphan_artifacts(
            list_artifacts=self._artifacts.list_refs, delete_artifact=self._artifacts.delete
        )

    def backup_workspace(self, destination: Path) -> None:
        """备份整个内容仓储及其引用产物，恢复时可由这些数据重建索引。"""
        self._lifecycle.backup_snapshot(
            lambda database, refs: write_backup(database, refs, self._artifacts, destination)
        )


@dataclass(frozen=True, slots=True)
class RecoveredPublication:
    """标识备份时已发布、可经重建索引后重新发布的文档版本。"""

    knowledge_base_id: str
    document_id: str
    revision_id: str


def restore_workspace(
    archive: Path, database: Path, artifacts: ArtifactStore
) -> tuple[RecoveredPublication, ...]:
    """还原到新的 SQLite 文件，核验引用并清除发布指针后再安装数据库。"""
    from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository

    if os.path.lexists(database):
        raise FileExistsError(database)
    if isinstance(artifacts, WorkspaceArtifactStore):
        artifacts.bind_workspace(database.resolve().as_uri())
    database.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".knowledge-restore-", dir=database.parent) as directory:
        staged_db = Path(directory) / "knowledge.sqlite"
        restore_backup(archive, staged_db, artifacts)
        repository = SQLiteKnowledgeRepository(staged_db)
        plan: list[RecoveredPublication] = []
        for catalog in repository.list_catalogs():
            offset = 0
            while page := repository.list_documents(catalog.id, offset=offset, limit=500):
                plan.extend(
                    RecoveredPublication(catalog.id, row.document_id, row.published_revision)
                    for row in page
                    if not row.deleted and row.published_revision is not None
                )
                offset += len(page)

        def verify_artifacts(_snapshot: Path, refs: Sequence[ArtifactRef]) -> None:
            for ref in refs:
                artifacts.read(ref)

        repository.backup_snapshot(verify_artifacts)
        repository.invalidate_restored_indexes()
        os.link(staged_db, database)
    return tuple(plan)
