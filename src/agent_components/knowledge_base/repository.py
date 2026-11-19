"""定义知识库的内容存储端口，隔离数据库和事务实现。"""

from collections.abc import Sequence
from typing import Protocol

from agent_components.knowledge_base.contracts import KnowledgeRevision


def _document_scope(document_ids: Sequence[str]) -> tuple[str, ...]:
    """读取完整文档 ID，防止将单个字符串拆成多个授权对象。"""
    if not isinstance(document_ids, Sequence) or isinstance(document_ids, (str, bytes, bytearray)):
        raise TypeError("document IDs must be a sequence of strings, such as a list or tuple")
    ids = tuple(document_ids)
    if any(not isinstance(value, str) for value in ids):
        raise TypeError("document IDs must be strings")
    return tuple(dict.fromkeys(ids))


class KnowledgeRepository(Protocol):
    """约定文档版本与发布指针的持久化语义。

    所有操作都以 knowledge_base_id 隔离。发布和取消发布须在仓储内执行原子版本检查。
    """

    def save_revision(self, knowledge_base_id: str, revision: KnowledgeRevision) -> None:
        """写入完整版本。重复提交相同内容幂等，同版本不同内容报版本冲突。"""
        ...

    def get_revision(
        self, knowledge_base_id: str, document_id: str, revision_id: str
    ) -> KnowledgeRevision | None:
        """读取指定版本，未发布版本也可通过此入口显式读取。"""
        ...

    def publish_revision(
        self,
        knowledge_base_id: str,
        document_id: str,
        revision_id: str,
        *,
        expected_revision: str | None,
    ) -> None:
        """将指针指向已有版本。expected_revision 为 None 时要求当前未发布。"""
        ...

    def unpublish_document(
        self, knowledge_base_id: str, document_id: str, *, expected_revision: str
    ) -> None:
        """原子移除匹配版本的发布指针，保留历史版本。"""
        ...

    def read_published(
        self, knowledge_base_id: str, *, document_ids: Sequence[str]
    ) -> tuple[KnowledgeRevision, ...]:
        """一致地读取指定文档的已发布版本。空序列表示本次没有可读文档。"""
        ...
