"""约定托管知识库的目录、文档产物与索引状态。"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from agent_components.knowledge_base.artifacts import ArtifactRef
from agent_components.knowledge_base.contracts import KnowledgeRevision
from agent_components.knowledge_base.repository import KnowledgeRepository

IndexJobStatus = Literal["queued", "running", "succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class Catalog:
    """标识知识库及其固定的索引和嵌入配置。"""

    id: str
    name: str
    index_id: str
    embedding_id: str
    version: int = 1
    generation: str | None = None


@dataclass(frozen=True, slots=True)
class RevisionArtifacts:
    """引用同一版本的源文件、解析结果和 Markdown。"""

    source: ArtifactRef
    parsed: ArtifactRef
    markdown: ArtifactRef


@dataclass(frozen=True, slots=True)
class IndexJob:
    """记录版本的索引任务和独立的尝试计数。"""

    id: str
    knowledge_base_id: str
    document_id: str
    revision_id: str
    status: IndexJobStatus
    attempts: int
    error: str | None


@dataclass(frozen=True, slots=True)
class DocumentStatus:
    """记录清单中的发布版本和删除标记。"""

    document_id: str
    published_revision: str | None
    deleted: bool


class LifecycleRepository(KnowledgeRepository, Protocol):
    """约定目录、内容、发布索引和删除标记之间的原子状态转换。"""

    @property
    def workspace_identity(self) -> str:
        """标识同一逻辑内容仓储的工作区。"""
        ...

    def create_catalog(self, catalog: Catalog) -> None:
        """创建一个尚未分配给其他目录的索引绑定。"""
        ...

    def get_catalog(self, kb_id: str) -> Catalog | None:
        """读取目录及其版本。"""
        ...

    def list_catalogs(self) -> tuple[Catalog, ...]:
        """按标识符顺序列出目录。"""
        ...

    def update_catalog(self, kb_id: str, name: str, *, expected_version: int) -> Catalog:
        """校验版本后更改名称并递增版本。"""
        ...

    def delete_catalog(self, kb_id: str, *, expected_version: int) -> None:
        """校验版本并删除已清理内容的目录。"""
        ...

    def save_managed_revision(
        self,
        kb_id: str,
        revision: KnowledgeRevision,
        create_artifacts: Callable[[], RevisionArtifacts],
        *,
        expected_generation: str | None = None,
    ) -> None:
        """创建产物及其不可变版本，重复提交同一版本为幂等操作。"""
        ...

    def get_artifacts(self, kb_id: str, doc_id: str, rev_id: str) -> RevisionArtifacts | None:
        """获取指定版本的产物引用。"""
        ...

    def list_documents(self, kb_id: str, *, offset: int, limit: int) -> tuple[DocumentStatus, ...]:
        """按文档标识符分页列出状态。"""
        ...

    def list_revisions(
        self, kb_id: str, doc_id: str, *, offset: int, limit: int
    ) -> tuple[str, ...]:
        """按版本标识符分页列出历史。"""
        ...

    def enqueue_index(
        self, kb_id: str, doc_id: str, rev_id: str, *, expected_generation: str | None = None
    ) -> IndexJob:
        """为版本创建索引任务，或返回已有任务。"""
        ...

    def get_index_job(self, kb_id: str, job_id: str) -> IndexJob | None:
        """获取目录中的索引任务。"""
        ...

    def list_index_jobs(
        self, kb_id: str, *, status: IndexJobStatus | None, offset: int, limit: int
    ) -> tuple[IndexJob, ...]:
        """按文档和版本分页查找索引任务，可按状态过滤。"""
        ...

    def claim_index(self, kb_id: str, job_id: str) -> IndexJob:
        """领取待执行或失败任务并递增尝试编号。"""
        ...

    def finish_index(
        self, kb_id: str, job_id: str, attempt: int, write_index: Callable[[], None]
    ) -> IndexJob:
        """为有效尝试写入索引并记录成功。"""
        ...

    def fail_index(self, kb_id: str, job_id: str, attempt: int, error: str) -> None:
        """为匹配的有效尝试记录失败。"""
        ...

    def reset_index(self, kb_id: str, job_id: str, *, expected_attempt: int) -> IndexJob:
        """在检查尝试编号和发布指针后重新排队。"""
        ...

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
        """核验版本索引和当前发布指针后切换发布版本。"""
        ...

    def unpublish_document(
        self,
        knowledge_base_id: str,
        document_id: str,
        *,
        expected_revision: str,
        expected_generation: str | None = None,
    ) -> None:
        """校验目录代次与发布版本后移除发布指针。"""
        ...

    def mark_deleted(
        self,
        kb_id: str,
        doc_id: str,
        *,
        expected_revision: str | None,
        expected_generation: str | None = None,
    ) -> None:
        """保留删除标记并移除当前发布指针。"""
        ...

    def purge_deleted(
        self,
        kb_id: str,
        doc_id: str,
        cleanup_vectors: Callable[[Sequence[str]], None],
        cleanup_artifact: Callable[[ArtifactRef], None],
        *,
        expected_generation: str | None = None,
    ) -> None:
        """移除已删除文档的索引、版本和不再引用的产物。"""
        ...

    def backup_snapshot(self, write_bundle: Callable[[Path, Sequence[ArtifactRef]], None]) -> None:
        """将同一时点的内容数据库和产物引用交给备份写入器。"""
        ...

    def collect_orphan_artifacts(
        self,
        *,
        list_artifacts: Callable[[], Sequence[ArtifactRef]],
        delete_artifact: Callable[[ArtifactRef], None],
    ) -> int:
        """统计并删除内容仓储中没有引用的产物。"""
        ...

    def invalidate_restored_indexes(self) -> None:
        """清除恢复库的发布指针并重新排队索引任务。"""
        ...
