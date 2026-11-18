"""演示解析、六类分块、父子块和长文本流式分块。

所有坐标都属于规范 Document.text，不能当作 PDF 字符坐标。
"""

import argparse
from hashlib import file_digest
from pathlib import Path

from agent_components.chunking import ChunkPolicy, ParentChildBuilder, StreamingChunker
from agent_components.parsing import DoclingParser
from examples._common import (
    chunker,
    document,
    emit,
    emit_lines,
    input_args,
    output_arg,
    protect,
    provider_args,
    read_json,
    run,
)


def main() -> None:
    """选择一个独立组件，读取真实输入并输出公共契约。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("parse", "native-pdf"):
        cmd = commands.add_parser(name)
        cmd.add_argument("input", type=Path)
        cmd.add_argument("--document-id")
        output_arg(cmd)
        if name == "parse":
            cmd.add_argument("--artifacts-path", type=Path)
            cmd.add_argument("--ocr", action="store_true")
            cmd.add_argument("--pages-per-batch", type=int)
    for name in ("chunk", "parents", "stream"):
        cmd = commands.add_parser(name)
        input_args(cmd)
        output_arg(cmd)
        provider_args(cmd)
        cmd.add_argument("--policy", type=Path, required=True)
        if name == "parents":
            cmd.add_argument(
                "--ranges", type=Path, help="可选的 [[start,end],...]；省略时使用结构标题"
            )
            cmd.add_argument("--max-level", type=int, default=6)
            cmd.add_argument("--exclude-preamble", action="store_true")
        if name == "stream":
            cmd.add_argument("--window-chars", type=int, default=65536)
            cmd.add_argument("--max-input-chars", type=int, default=100_000_000)
            cmd.add_argument("--max-chunks", type=int, default=1_000_000)
    args = parser.parse_args()
    protect(args.output, args.input, getattr(args, "policy", None), getattr(args, "ranges", None))
    if args.command == "parse":
        backend = DoclingParser(artifacts_path=args.artifacts_path, do_ocr=args.ocr)
        if args.pages_per_batch is not None:
            emit_lines(
                backend.iter_pdf(
                    args.input, document_id=args.document_id, pages_per_batch=args.pages_per_batch
                ),
                args.output,
            )
        else:
            emit(backend.parse(args.input, document_id=args.document_id), args.output)
        return
    if args.command == "native-pdf":
        # 原生管线无需模型权重；仍会导入 Docling/Torch，不提供版面/表格结构恢复。
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter, NativePdfFormatOption

        converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={InputFormat.PDF: NativePdfFormatOption()},
        )
        with args.input.open("rb") as source:
            revision_id = file_digest(source, "sha256").hexdigest()
        native = converter.convert(args.input, raises_on_error=True).document
        emit(
            DoclingParser().from_native(
                native,
                document_id=args.document_id or args.input.name,
                revision_id=revision_id,
                filename=args.input.name,
            ),
            args.output,
        )
        return
    policy = ChunkPolicy.model_validate(read_json(args.policy))
    with chunker(args) as backend:
        if args.command == "stream":
            if args.input_kind != "text":
                raise ValueError("stream accepts only UTF-8 canonical text")
            stream = StreamingChunker(
                backend,
                window_chars=args.window_chars,
                max_input_chars=args.max_input_chars,
                max_chunks=args.max_chunks,
            )
            emit_lines(
                stream.iter_file(
                    args.input, policy, document_id=args.document_id or args.input.name
                ),
                args.output,
            )
            return
        source = document(args.input, args.input_kind, args.document_id)
        if args.command == "chunk":
            result = backend.chunk(source, policy)
        elif args.ranges:
            result = ParentChildBuilder(backend).build(source, read_json(args.ranges), policy)
        else:
            result = ParentChildBuilder(backend).build_sections(
                source, policy, max_level=args.max_level, include_preamble=not args.exclude_preamble
            )
        emit(result, args.output)


if __name__ == "__main__":
    run(main)
