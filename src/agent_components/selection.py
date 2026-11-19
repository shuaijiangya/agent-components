"""保留旧的检索候选选择导入路径。"""

from agent_components.retrieval.selection import (
    RetrievalCandidate,
    SelectionDrop,
    SelectionResult,
    deduplicate,
    mmr_select,
)

__all__ = ["RetrievalCandidate", "SelectionDrop", "SelectionResult", "deduplicate", "mmr_select"]
