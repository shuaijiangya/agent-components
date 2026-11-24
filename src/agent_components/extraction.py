"""保留独立抽取组件的旧导入路径与证据注入行为。"""

from agent_components.core.ports import EvidenceBackend, StructuredModel
from agent_components.support.extraction import Extractor as SupportExtractor


class Extractor(SupportExtractor):
    """兼容已使用旧构造器的调用方。"""

    _legacy = True

    def __init__(
        self, model: StructuredModel, *, evidence_backend: EvidenceBackend | None = None
    ) -> None:
        """绑定结构化模型及调用方显式传入的证据后端。"""
        super().__init__(model, evidence_backend=evidence_backend)
