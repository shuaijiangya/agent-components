"""定义由知识库内容仓储保存的文档版本。"""

from typing import Self

from pydantic import model_validator

from agent_components.core.contracts import ChunkResult, Contract, Document


class KnowledgeRevision(Contract):
    """保存一份规范文档及其完整分块产物。

    文档版本同时标识这一份分块结果。重新分块时应在分块前分配新的版本 ID。
    """

    document: Document
    chunks: ChunkResult

    @model_validator(mode="after")
    def validate_alignment(self) -> Self:
        """校验文档版本、规范文本范围和父子关系。"""
        document = self.document
        if not document.id.strip() or not document.revision_id.strip():
            raise ValueError("document and revision IDs must not be blank")
        if self.chunks.input_length != len(document.text):
            raise ValueError("chunk result length must match the document")
        by_id = {chunk.id: chunk for chunk in self.chunks.chunks}
        if len(by_id) != len(self.chunks.chunks):
            raise ValueError("chunk IDs must be unique within a revision")
        ordinals = set()
        for chunk in self.chunks.chunks:
            if (chunk.document_id, chunk.revision_id) != (document.id, document.revision_id):
                raise ValueError("chunk does not belong to the document revision")
            if (
                chunk.end > len(document.text)
                or chunk.text != document.text[chunk.start : chunk.end]
            ):
                raise ValueError("chunk offsets and text must match canonical text")
            ordinal = (chunk.kind, chunk.ordinal)
            if ordinal in ordinals:
                raise ValueError("chunk ordinals must be unique within their kind")
            ordinals.add(ordinal)
            if chunk.parent_id is not None:
                parent = by_id.get(chunk.parent_id)
                if (
                    chunk.kind != "leaf"
                    or parent is None
                    or parent.kind != "parent"
                    or not parent.start <= chunk.start <= chunk.end <= parent.end
                ):
                    raise ValueError("chunk parent must contain the leaf in the same revision")
        return self
