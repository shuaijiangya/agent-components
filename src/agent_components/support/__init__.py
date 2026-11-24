"""为业务开发者提供可独立调用的结构化数据和证据抽取组件。"""

from agent_components.core.contracts import (
    Evidence,
    EvidenceOptions,
    ExampleCitation,
    ExtractionCoverage,
    ExtractionExample,
    ExtractionMeta,
    ExtractionOptions,
    ExtractionResult,
)
from agent_components.support.extraction import Extractor

__all__ = [
    "Evidence",
    "EvidenceOptions",
    "ExampleCitation",
    "ExtractionCoverage",
    "ExtractionExample",
    "ExtractionMeta",
    "ExtractionOptions",
    "ExtractionResult",
    "Extractor",
]
