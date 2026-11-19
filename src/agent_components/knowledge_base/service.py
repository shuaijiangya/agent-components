"""提供基于仓储端口的知识库版本管理。"""

from collections.abc import Sequence

from agent_components.core.contracts import Chunk, ChunkResult, Document
from agent_components.knowledge_base.contracts import KnowledgeRevision
from agent_components.knowledge_base.repository import KnowledgeRepository, _document_scope


class KnowledgeBase:
    """管理一个知识库内的规范文档、分块和发布指针。

    调用方维护用户权限、向量入库时机和存储连接。内容可先保存为草稿，再显式发布。
    """

    def __init__(self, knowledge_base_id: str, repository: KnowledgeRepository) -> None:
        """绑定知识库标识与内容仓储。"""
        if not knowledge_base_id.strip():
            raise ValueError("knowledge_base_id must not be blank")
        self._id = knowledge_base_id
        self._repository = repository

    def save_revision(self, document: Document, chunks: ChunkResult) -> None:
        """验证分块归属并持久化版本，发布指针保持原值。"""
        revision = KnowledgeRevision(document=document, chunks=chunks)
        self._repository.save_revision(self._id, revision)

    def get_revision(self, document_id: str, revision_id: str) -> KnowledgeRevision | None:
        """读取草稿或历史版本，调用方负责读取授权。"""
        return self._repository.get_revision(self._id, document_id, revision_id)

    def publish_revision(
        self, document_id: str, revision_id: str, *, expected_revision: str | None
    ) -> None:
        """以版本条件发布已有版本。调用方应先完成该版本的向量写入。"""
        self._repository.publish_revision(
            self._id, document_id, revision_id, expected_revision=expected_revision
        )

    def unpublish_document(self, document_id: str, *, expected_revision: str) -> None:
        """停止发布当前版本，使后续请求无法从已发布语料中读到它。"""
        self._repository.unpublish_document(
            self._id, document_id, expected_revision=expected_revision
        )

    def published_chunks(self, *, document_ids: Sequence[str]) -> tuple[Chunk, ...]:
        """读取应用已授权文档的当前分块，作为一次检索的发布快照。

        父子块及来源结构完整保留。如果权限细到块，应用还应按块过滤。
        """
        revisions = self._repository.read_published(
            self._id, document_ids=_document_scope(document_ids)
        )
        return tuple(chunk for revision in revisions for chunk in revision.chunks.chunks)
