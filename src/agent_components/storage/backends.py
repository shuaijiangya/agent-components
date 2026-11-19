"""按运行时配置独立选择内容仓储和向量存储。"""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import Field

from agent_components.core.contracts import Contract
from agent_components.knowledge_base.repository import KnowledgeRepository
from agent_components.retrieval.vector_store import VectorStore


class StorageBinding(Contract):
    """描述当前请求使用的两个已注册适配器名称。"""

    documents: str = Field(min_length=1)
    vectors: str = Field(min_length=1)


@dataclass(frozen=True)
class StorageBackends[DocumentsT: KnowledgeRepository, VectorsT: VectorStore]:
    """保存一次解析得到的存储对象，供同一次业务请求使用。"""

    documents: DocumentsT
    vectors: VectorsT


class StorageRouter[DocumentsT: KnowledgeRepository, VectorsT: VectorStore]:
    """从应用显式注册的实例解析存储组合。

    应用可以在新请求读取新的绑定配置。已解析的请求持有原有实例。
    各适配器连接、配置来源及切换前的数据准备由应用管理。
    """

    def __init__(
        self, *, documents: Mapping[str, DocumentsT], vectors: Mapping[str, VectorsT]
    ) -> None:
        """绑定两个独立的适配器集合。"""
        self._documents = dict(documents)
        self._vectors = dict(vectors)

    def resolve(self, binding: StorageBinding) -> StorageBackends[DocumentsT, VectorsT]:
        """从同一绑定配置解析两个后端，未注册时抛出 KeyError。"""
        if binding.documents not in self._documents:
            raise KeyError(f"unregistered documents backend: {binding.documents}")
        if binding.vectors not in self._vectors:
            raise KeyError(f"unregistered vectors backend: {binding.vectors}")
        return StorageBackends(self._documents[binding.documents], self._vectors[binding.vectors])
