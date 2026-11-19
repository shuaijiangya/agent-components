"""提供知识库内容、版本发布和可替换的仓储接口。"""

from agent_components.knowledge_base.contracts import KnowledgeRevision
from agent_components.knowledge_base.repository import KnowledgeRepository
from agent_components.knowledge_base.service import KnowledgeBase

__all__ = ["KnowledgeBase", "KnowledgeRepository", "KnowledgeRevision"]
