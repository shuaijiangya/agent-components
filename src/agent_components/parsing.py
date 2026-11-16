"""通过延迟加载的 Docling Adapter 解析文件。

输出以 Markdown 为规范文本，并在部件序列化时建立元素、页码和矩形来源映射。
"""

import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue

from agent_components.core.contracts import (
    Document,
    DocumentElement,
    ParsedDocument,
    SourceRef,
    SourceSpan,
)
from agent_components.core.errors import MissingDependencyError


class DoclingParser:
    """将文件解析成统一 Document 和后端原生结构。

    TXT 使用 UTF-8 BOM 兼容读取；其他已支持扩展名委托 Docling，PDF 使用 CPU。
    """

    def __init__(
        self,
        *,
        artifacts_path: Path | None = None,
        allow_model_downloads: bool = False,
        do_ocr: bool = False,
    ) -> None:
        """配置解析资源和 PDF 行为。

        Args:
            artifacts_path: PDF 所需离线模型目录。
            allow_model_downloads: 是否允许 PDF 未指定本地目录时由后端获取模型。
            do_ocr: 是否启用 PDF OCR；默认关闭。
        """
        self._artifacts = artifacts_path
        self._allow_downloads = allow_model_downloads
        self._do_ocr = do_ocr

    def parse(self, path: Path, *, document_id: str | None = None) -> ParsedDocument:
        """读取本地文件并构造规范文本。

        Args:
            path: 本地文件路径。
            document_id: 调用方定义的文档身份；省略时使用文件 SHA-256。

        Returns:
            Document、Markdown、原生 JSON；revision_id 为文件内容摘要。

        Raises:
            ValueError: 不支持扩展名或离线 PDF 缺少现有模型目录。
            MissingDependencyError: 非 TXT 解析未安装 parsing extra。
            OSError: 文件读取失败。

        Notes:
            来源按原生元素精度映射；存在目录不代表已验证全部 PDF 权重完整。
        """
        path = Path(path)
        with path.open("rb") as reader:
            digest = hashlib.file_digest(reader, "sha256").hexdigest()
        document_id = document_id or digest
        if path.suffix.lower() == ".txt":
            with path.open(encoding="utf-8-sig", newline="") as reader:
                text = reader.read()
            return ParsedDocument(
                document=Document(
                    id=document_id,
                    text=text,
                    revision_id=digest,
                    metadata={"filename": path.name, "format": "text"},
                ),
                markdown=text,
                native_json={"adapter": "plain_text", "text": text},
            )
        converter = self._converter(path)
        result = converter.convert(path, raises_on_error=True)
        return self.from_native(
            result.document, document_id=document_id, revision_id=digest, filename=path.name
        )

    def _converter(self, path: Path) -> Any:
        """构造 CPU 转换器并在 PDF 入口执行离线资源约束。

        Args:
            path: 用于选择原生格式和检查 PDF 资源的路径。

        Returns:
            尚未执行文件转换的原生 DocumentConverter。
        """
        if path.suffix.lower() == ".pdf" and not self._allow_downloads:
            if self._artifacts is None or not self._artifacts.is_dir():
                raise ValueError("Offline PDF parsing requires an existing artifacts_path")
        try:
            from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as exc:
            raise MissingDependencyError("parsing") from exc
        formats = {
            ".pdf": InputFormat.PDF,
            ".md": InputFormat.MD,
            ".html": InputFormat.HTML,
            ".htm": InputFormat.HTML,
            ".docx": InputFormat.DOCX,
            ".pptx": InputFormat.PPTX,
            ".xlsx": InputFormat.XLSX,
        }
        if path.suffix.lower() not in formats:
            raise ValueError(f"Unsupported file extension: {path.suffix}")
        options = PdfPipelineOptions(
            artifacts_path=self._artifacts,
            do_ocr=self._do_ocr,
            accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU),
        )
        return DocumentConverter(
            allowed_formats=[formats[path.suffix.lower()]],
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
        )

    def iter_parse(self, paths: Iterable[Path]) -> Iterator[ParsedDocument]:
        """逐文件解析，避免同时保留整批文档。

        Args:
            paths: 惰性文件路径序列。

        Yields:
            当前文件的解析产物；Docling 单文件转换仍由原生引擎管理内存。
        """
        for path in paths:
            yield self.parse(path)

    def iter_pdf(
        self,
        path: Path,
        *,
        document_id: str | None = None,
        pages_per_batch: int = 10,
    ) -> Iterator[ParsedDocument]:
        """按 PDF 页范围分批解析，限制单次转换的页数。

        Args:
            path: PDF 文件路径。
            document_id: 原文件身份；省略时使用文件内容摘要。
            pages_per_batch: 单次解析的最大页数，必须为正整数。

        Yields:
            具有独立分段身份的 ParsedDocument，元数据记录原文件身份和全局页范围。
            元素来源页码由 Docling 提供，不将跨批表格或章节自行拼接。

        Raises:
            ValueError: 格式、批大小无效或离线资源未配置。
            MissingDependencyError: 未安装解析后端。

        Notes:
            原生引擎可能缓存模型和页面元数据；此接口限制转换范围，不承诺固定 RSS。
        """
        path = Path(path)
        if path.suffix.lower() != ".pdf" or type(pages_per_batch) is not int or pages_per_batch < 1:
            raise ValueError("iter_pdf requires a PDF and positive pages_per_batch")
        converter = self._converter(path)
        try:
            import pypdfium2
        except ImportError as exc:
            raise MissingDependencyError("parsing") from exc
        with path.open("rb") as reader:
            digest = hashlib.file_digest(reader, "sha256").hexdigest()
        pdf = pypdfium2.PdfDocument(str(path))
        try:
            page_count = len(pdf)
        finally:
            pdf.close()
        source_id = document_id or digest
        for first in range(1, page_count + 1, pages_per_batch):
            last = min(first + pages_per_batch - 1, page_count)
            converted = converter.convert(path, raises_on_error=True, page_range=(first, last))
            parsed = self.from_native(
                converted.document,
                document_id=f"{source_id}#pages={first}-{last}",
                revision_id=digest,
                filename=path.name,
            )
            # 分批产物不能共用全局 ordinal；独立分段 ID 防止索引和上下文版本混用。
            document = parsed.document.model_copy(
                update={
                    "metadata": {
                        **parsed.document.metadata,
                        "source_document_id": source_id,
                        "page_start": first,
                        "page_end": last,
                    }
                }
            )
            yield parsed.model_copy(update={"document": document})

    def from_native(
        self, native: Any, *, document_id: str, revision_id: str, filename: str = ""
    ) -> ParsedDocument:
        """把原生 DoclingDocument 转换为可映射的规范 Markdown。

        Args:
            native: 已完成解析的原生 DoclingDocument，可来自离线转换或缓存。
            document_id: 调用方的文档身份。
            revision_id: 原文件或原生 JSON 的内容版本。
            filename: 可选的原文件名。

        Returns:
            具有结构元素和 SourceMap 的文档；映射精度明确为原生元素级。

        Raises:
            MissingDependencyError: 未安装 parsing extra。
        """
        try:
            from docling_core.transforms.serializer.markdown import MarkdownDocSerializer
        except ImportError as exc:
            raise MissingDependencyError("parsing") from exc
        from agent_components.tables import markdown_table_ranges

        serializer = MarkdownDocSerializer(doc=native)
        blocks: list[str] = []
        source_map: list[SourceSpan] = []
        elements: list[DocumentElement] = []
        position = 0
        # 直接消费原生序列化部件并累计偏移；重复段落不会被首次 find() 错配。
        visited: set[str] = set()
        # DOCX 常把整章放进 Group；逐个原生 DocItem 序列化，避免整章被误记为一个标题。
        for item, _ in native.iterate_items():
            if item.self_ref in visited:
                continue
            part = serializer.serialize(item=item, visited=visited)
            if not part.text:
                continue
            if blocks:
                position += 2
            start, end = position, position + len(part.text)
            items = part.get_unique_doc_items()
            refs: list[SourceRef] = []
            for item in items:
                provenances: list[Any] = list(item.prov) or [None]
                for prov in provenances:
                    bbox = None if prov is None else prov.bbox
                    refs.append(
                        SourceRef(
                            input_id=document_id,
                            document_id=document_id,
                            revision_id=revision_id,
                            element_id=item.self_ref,
                            page=None if prov is None else prov.page_no,
                            precision="element",
                            bbox=None if bbox is None else (bbox.l, bbox.t, bbox.r, bbox.b),
                            coord_origin=None if bbox is None else bbox.coord_origin.value,
                        )
                    )
            if refs:
                source_map.append(SourceSpan(start=start, end=end, refs=tuple(refs)))
            headings = [item for item in items if item.label.value in {"title", "section_header"}]
            tables = [item for item in items if item.label.value == "table"]
            if headings:
                heading = headings[0]
                elements.append(
                    DocumentElement(
                        id=heading.self_ref,
                        kind="heading",
                        start=start,
                        end=end,
                        level=getattr(heading, "level", 1),
                        title=getattr(heading, "text", part.text),
                    )
                )
            elif tables:
                ranges = markdown_table_ranges(part.text)
                if not ranges:
                    # 无法表示为 GFM 的表格仍作为原子元素保留，交由表格预算规则处理。
                    ranges = [(0, len(part.text))]
                for index, (left, right) in enumerate(ranges):
                    elements.append(
                        DocumentElement(
                            id=f"{tables[0].self_ref}:{index}",
                            kind="table",
                            start=start + left,
                            end=start + right,
                        )
                    )
            else:
                elements.append(
                    DocumentElement(
                        id=items[0].self_ref if items else f"block:{len(blocks)}",
                        kind="text",
                        start=start,
                        end=end,
                    )
                )
            blocks.append(part.text)
            position = end
        text = "\n\n".join(blocks)
        return ParsedDocument(
            document=Document(
                id=document_id,
                text=text,
                revision_id=revision_id,
                metadata={
                    "filename": filename,
                    "text_representation": "docling_markdown",
                    "canonical_serializer": "docling-elements-v1",
                },
                elements=tuple(elements),
                source_map=tuple(source_map),
            ),
            markdown=text,
            native_json=cast(dict[str, JsonValue], native.export_to_dict()),
            source_mapping="mapped" if source_map else "text_only",
        )
