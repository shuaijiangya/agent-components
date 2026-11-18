"""以磁盘快照和有界窗口处理超大规范文本。

支持全部六种分块策略；表格逐行处理，跨窗口保持表头。原生 PDF/Office
解析仍遵循 Docling 的单文件内存模型，可通过 iter_parse 逐文件消费。
"""

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TextIO

from agent_components.chunking.chunker import Chunker, ChunkPolicy, _chunk, _validated_policy
from agent_components.chunking.tables import SpanPiece
from agent_components.core.contracts import Chunk, Document, TableSlice
from agent_components.core.markdown_tables import TABLE_SEPARATOR


class StreamingChunker:
    """将单个规范文本流先快照，再按窗口惰性产出有全局偏移的块。

    快照阶段先确定完整内容摘要，避免产出后才知道身份；临时文件在关闭迭代器时清理。
    """

    def __init__(
        self,
        chunker: Chunker | None = None,
        *,
        window_chars: int = 65536,
        max_input_chars: int = 100_000_000,
        max_chunks: int = 1_000_000,
    ) -> None:
        """配置窗口和资源硬上限。

        Args:
            chunker: 可注入 Token/向量能力的分块器。
            window_chars: 普通文本分析窗口及单行表格读取上限。
            max_input_chars: 单个输入的最大字符数，超过即停止快照。
            max_chunks: 最多产出的块数，限制极小 size 导致的产物膨胀。

        Raises:
            ValueError: 任一资源参数不是正整数。
        """
        for value in (window_chars, max_input_chars, max_chunks):
            if type(value) is not int or value < 1:
                raise ValueError("stream resource limits must be positive integers")
        self._chunker = chunker or Chunker()
        self._window = window_chars
        self._max_input = max_input_chars
        self._max_chunks = max_chunks

    def iter_file(
        self,
        path: Path,
        policy: ChunkPolicy,
        *,
        document_id: str,
        revision_id: str | None = None,
    ) -> Iterator[Chunk]:
        """流式读取 UTF-8/BOM 文本文件并保留 CRLF。

        Args:
            path: 规范文本文件，不在此处调用 PDF/Office 转换。
            policy: 六种策略之一。
            document_id: 使用方定义的稳定文档身份。
            revision_id: 可选版本；省略时使用规范文本摘要。

        Yields:
            正文和偏移可从完整规范文本验证的块。

        Raises:
            ValueError: 快照读取期间文件发生变化或资源上限被突破。
        """

        def reads() -> Iterator[str]:
            with Path(path).open(encoding="utf-8-sig", newline="") as reader:
                before = os.fstat(reader.fileno())
                while value := reader.read(self._window):
                    yield value
                after = os.fstat(reader.fileno())
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValueError("source file changed while taking snapshot")

        yield from self.iter_text(reads(), policy, document_id=document_id, revision_id=revision_id)

    def iter_text(
        self,
        pieces: Iterable[str],
        policy: ChunkPolicy,
        *,
        document_id: str,
        revision_id: str | None = None,
    ) -> Iterator[Chunk]:
        """快照规范文本流并逐块返回。

        Args:
            pieces: 任意传输边界的 Unicode 字符串序列。
            policy: 分块配置，所有策略共享全局身份及偏移。
            document_id: 原文档身份。
            revision_id: 可选版本，默认使用完整规范文本摘要。

        Yields:
            每块具有全局 ordinal、start/end、来源范围及可选表格信息。

        Raises:
            ValueError: 输入非字符串、资源超限、表格单行超过窗口或所选策略失败。

        Notes:
            产出中途仍可能遇到后端错误；使用方应在全次成功后原子发布索引批次。
        """
        policy = _validated_policy(policy)
        Document(id=document_id, text="")
        if policy.overlap >= self._window:
            raise ValueError("streaming window must exceed overlap")
        base = self._chunker.configuration_fingerprint(policy)
        fingerprint = hashlib.sha256(
            json.dumps([base, "stream-v1", self._window]).encode()
        ).hexdigest()
        digest = hashlib.sha256()
        length = 0
        # 磁盘快照不保留整篇 Python 字符串；先完成身份和长度校验再产出首块。
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", newline="") as snapshot:
            for piece in pieces:
                if not isinstance(piece, str):
                    raise ValueError("stream input must contain strings")
                length += len(piece)
                if length > self._max_input:
                    raise ValueError("stream exceeds max_input_chars")
                for start in range(0, len(piece), self._window):
                    part = piece[start : start + self._window]
                    digest.update(part.encode())
                    snapshot.write(part)
            content_digest = digest.hexdigest()
            snapshot.seek(0)
            ordinal = 0
            emitted_end = 0
            previous_tail = ""
            previous_end = 0
            for origin, value, prefix, table in self._segments(snapshot, policy):
                if table is None and policy.overlap and previous_tail and previous_end == origin:
                    # 分析窗口是资源边界，不应让普通文本的重叠在窗口接缝处消失。
                    origin -= len(previous_tail)
                    value = previous_tail + value
                previous_end = origin + len(value)
                previous_tail = ""
                if table is None and policy.overlap:
                    if policy.strategy == "token":
                        probe = max(64, policy.overlap * 16)
                        for length in range(1, min(len(value), probe) + 1):
                            if self._chunker._measure(value[-length:], policy) <= policy.overlap:
                                previous_tail = value[-length:]
                    else:
                        previous_tail = value[-policy.overlap :]
                document = Document(
                    id=document_id,
                    revision_id=content_digest if revision_id is None else revision_id,
                    text=value,
                )
                if table is None:
                    local_policy = policy.model_copy(update={"preserve_tables": False})
                    parts = self._chunker._pieces(document, local_policy)
                else:
                    parts = [SpanPiece(0, len(value), prefix, table)]
                for chunk_piece in parts:
                    if origin + chunk_piece.end <= emitted_end:
                        continue
                    if ordinal >= self._max_chunks:
                        raise ValueError("stream exceeds max_chunks")
                    yield _chunk(
                        document,
                        chunk_piece.start,
                        chunk_piece.end,
                        ordinal,
                        fingerprint,
                        content_digest,
                        prefix=chunk_piece.prefix,
                        table=chunk_piece.table,
                        offset_base=origin,
                    )
                    emitted_end = origin + chunk_piece.end
                    ordinal += 1

    def _segments(
        self,
        reader: TextIO,
        policy: ChunkPolicy,
    ) -> Iterator[tuple[int, str, str, TableSlice | None]]:
        """以有界普通窗口和逐行表格段读取快照。

        Args:
            reader: 定位到快照开头的文本读取器。
            policy: 原始配置，用于表格预算和溢出行为。

        Yields:
            原文起点、连续正文、独立前缀及可选表格元数据。
        """
        position = 0
        buffer = ""
        buffer_start = 0
        pending: tuple[int, str] | None = None
        in_fence: tuple[str, int] | None = None
        continuation = False
        line_starts: dict[int, bool] = {}

        def read() -> tuple[int, str]:
            nonlocal position, continuation
            start = position
            line_starts[start] = not continuation
            line = reader.readline(self._window + 1)
            continuation = (
                bool(line) and not line.endswith(("\n", "\r")) and len(line) > self._window
            )
            position += len(line)
            return start, line

        while True:
            origin, line = pending if pending is not None else read()
            pending = None
            if not line:
                break
            at_line_start = line_starts.pop(origin, True)
            marker = (
                re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
                if at_line_start
                else None
            )
            if marker:
                symbol, tail = marker.groups()
                if in_fence is None:
                    in_fence = (symbol[0], len(symbol))
                elif symbol[0] == in_fence[0] and len(symbol) >= in_fence[1] and not tail.strip():
                    in_fence = None
            candidate = (
                policy.preserve_tables
                and at_line_start
                and in_fence is None
                and "|" in line
                and len(line) <= self._window
            )
            if candidate:
                next_origin, next_line = read()
                if TABLE_SEPARATOR.fullmatch(next_line.strip()):
                    if len(next_line) > self._window:
                        raise ValueError("table header exceeds streaming window")
                    if buffer:
                        yield buffer_start, buffer, "", None
                        buffer = ""
                    line_starts.pop(next_origin, None)
                    header = line + next_line
                    header_end = origin + len(header)
                    current, current_start, row, first_row = header, origin, 0, 0
                    repeated = header if header.endswith(("\n", "\r")) else header + "\n"
                    prefix = ""
                    while True:
                        row_origin, body = read()
                        if not body or not body.strip() or "|" not in body:
                            pending = (row_origin, body)
                            break
                        line_starts.pop(row_origin, None)
                        if len(body) > self._window:
                            raise ValueError("table row exceeds streaming window")
                        if row > first_row and (
                            self._chunker._measure(prefix + current + body, policy) > policy.size
                            or len(prefix) + len(current) + len(body) > self._window
                        ):
                            oversized = (
                                self._chunker._measure(prefix + current, policy) > policy.size
                            )
                            yield (
                                current_start,
                                current,
                                prefix,
                                TableSlice(
                                    table_id=f"markdown-table:{origin}",
                                    row_start=first_row,
                                    row_end=row,
                                    header_start=origin,
                                    header_end=header_end,
                                    oversized=oversized,
                                ),
                            )
                            current, current_start, prefix, first_row = (
                                "",
                                row_origin,
                                repeated,
                                row,
                            )
                        current += body
                        row += 1
                        if (
                            self._chunker._measure(prefix + current, policy) > policy.size
                            and policy.table_overflow == "error"
                        ):
                            raise ValueError("table header plus one row exceeds size")
                    oversized = self._chunker._measure(prefix + current, policy) > policy.size
                    if oversized and policy.table_overflow == "error":
                        raise ValueError("table header exceeds size")
                    yield (
                        current_start,
                        current,
                        prefix,
                        TableSlice(
                            table_id=f"markdown-table:{origin}",
                            row_start=first_row,
                            row_end=row,
                            header_start=origin,
                            header_end=header_end,
                            oversized=oversized,
                        ),
                    )
                    buffer_start = position
                    continue
                pending = (next_origin, next_line)
            if not buffer:
                buffer_start = origin
            buffer += line
            while len(buffer) >= self._window:
                yield buffer_start, buffer[: self._window], "", None
                buffer_start += self._window
                buffer = buffer[self._window :]
        if buffer:
            yield buffer_start, buffer, "", None
