"""从已保存的解析产物生成可追溯的分块版本。"""

import hashlib
import json

from agent_components.chunking.chunker import Chunker, ChunkPolicy, ParentChildBuilder
from agent_components.chunking.sections import _SECTION_LAYOUT_VERSION, SectionOptions
from agent_components.core.contracts import (
    ChunkResult,
    Contract,
    Document,
    ParsedDocument,
    ParseProvenance,
)

_TRACE_METADATA_KEY = "agent_components.rechunk"


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RechunkTrace(Contract):
    """记录分块版本的输入产物、配置以及可选的前一版本。

    artifact_sha256 对规范 JSON 计算，等价的键顺序和格式不会改变摘要。
    parsing=None 表示历史产物缺少解析记录。artifact_ref 由调用方提供并管理。
    """

    source_document_id: str
    source_revision_id: str
    artifact_sha256: str
    artifact_ref: str | None = None
    previous_revision_id: str | None = None
    parsing: ParseProvenance | None = None
    chunk_policy: ChunkPolicy
    section_options: SectionOptions | None = None
    backend_ids: dict[str, str]
    chunk_config_fingerprint: str

    def matches_artifact(self, parsed: ParsedDocument) -> bool:
        """检查读取的解析产物是否为本次分块使用的原始快照。"""
        return _digest(parsed.model_dump(mode="json")) == self.artifact_sha256


class RechunkedDocument(Contract):
    """返回可直接保存的文档、分块及追溯记录。"""

    document: Document
    chunks: ChunkResult
    trace: RechunkTrace


class Rechunker:
    """为已解析内容生成独立版本，并复用与普通分块相同的 Provider。"""

    def __init__(self, chunker: Chunker | None = None) -> None:
        """绑定可选的分块器及其实际 Tokenizer 或 Embedding Provider。"""
        self._chunker = chunker or Chunker()

    def rechunk(
        self,
        parsed: ParsedDocument,
        policy: ChunkPolicy,
        *,
        section_options: SectionOptions | None = None,
        artifact_ref: str | None = None,
        previous_revision_id: str | None = None,
    ) -> RechunkedDocument:
        """只读取解析产物，产生可与旧分块共存的版本。

        Args:
            parsed: 原先保存的 ParsedDocument，可通过 JSON 恢复，无需原文件或解析器。
            policy: 这次使用的分块策略，包含实际策略参数。
            section_options: 提供时生成有界章节父子块，省略时仅生成叶子。
            artifact_ref: 调用方稳定保存的产物引用。组件不访问该引用。
            previous_revision_id: 可选的前一次分块版本，由调用方指定。

        Returns:
            具有派生版本 ID 的 Document、ChunkResult 及追溯记录。
            同一产物、追溯引用、策略与 Provider 标识会得到相同版本 ID。
        """
        policy = ChunkPolicy.model_validate(policy.model_dump())
        if section_options is not None:
            section_options = SectionOptions.model_validate(section_options.model_dump())
        # Contract 只冻结外层字段。摘要、解析配置及派生文档都从隔离后的同一快照读取。
        parsed = parsed.model_copy(deep=True)
        if _TRACE_METADATA_KEY in parsed.document.metadata:
            raise ValueError(f"source metadata uses reserved key {_TRACE_METADATA_KEY!r}")
        artifact_digest = _digest(parsed.model_dump(mode="json"))
        base_config = self._chunker.configuration_fingerprint(policy)
        revision_digest = _digest(
            [
                "rechunk-v1",
                artifact_digest,
                base_config,
                None
                if section_options is None
                else [
                    _SECTION_LAYOUT_VERSION,
                    section_options.model_dump(mode="json"),
                ],
                artifact_ref,
                previous_revision_id,
            ]
        )
        doc = parsed.document.model_copy(update={"revision_id": f"rechunk-{revision_digest}"})
        if section_options is None:
            chunks = self._chunker.chunk(doc, policy)
        else:
            chunks = ParentChildBuilder(self._chunker).build_sections(
                doc,
                policy,
                max_level=section_options.max_level,
                include_preamble=section_options.include_preamble,
                max_parent_chars=section_options.max_parent_chars,
            )
        trace = RechunkTrace(
            source_document_id=parsed.document.id,
            source_revision_id=parsed.document.revision_id,
            artifact_sha256=artifact_digest,
            artifact_ref=artifact_ref,
            previous_revision_id=previous_revision_id,
            parsing=parsed.provenance,
            chunk_policy=policy,
            section_options=section_options,
            backend_ids=self._chunker._backend_identity(policy),
            chunk_config_fingerprint=chunks.config_fingerprint,
        )
        # 文件来源仍指向原解析产物。规范文本引用由分块器写入新的文档版本。
        doc = doc.model_copy(
            update={
                "metadata": {**doc.metadata, _TRACE_METADATA_KEY: trace.model_dump(mode="json")}
            }
        )
        return RechunkedDocument(document=doc, chunks=chunks, trace=trace)
