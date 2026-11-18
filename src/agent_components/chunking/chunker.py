"""提供独立的文档分块组件，不依赖 Agent、存储或业务流程。

字符策略由本组件实现；递归策略委托 Chonkie。所有偏移均为规范文本的
Python 字符下标，采用左闭右开区间，不表示原文件字节或 PDF 页内坐标。
"""

import hashlib
import json
from bisect import bisect_right
from collections.abc import Sequence
from itertools import islice
from typing import Literal, Self

from pydantic import Field, model_validator

from agent_components.chunking.backends import (
    EmbeddingProvider,
    TokenEncoder,
    literal_spans,
    semantic_spans,
    sentence_spans,
    token_count,
    token_spans,
)
from agent_components.chunking.sections import SectionBuilder
from agent_components.chunking.sources import SourceMapper
from agent_components.chunking.tables import SpanPiece, split_table
from agent_components.core.contracts import (
    Chunk,
    ChunkResult,
    Contract,
    Document,
    Issue,
    TableSlice,
)
from agent_components.core.errors import ChunkAlignmentError, MissingDependencyError
from agent_components.core.markdown_tables import markdown_table_ranges

_IMPLEMENTATION_VERSION = "chunking-v3"
_RECURSIVE_DELIMITERS = (("\n\n", "\n"), ("。", "！", "？", ". ", "! ", "? "))


class ChunkPolicy(Contract):
    """声明分块策略、计量单位和结构保持规则。

    Attributes:
        strategy: character 为固定字符窗口；recursive 为 Chonkie 递归规则。
        size: token 策略为真实 Token 上限，其余策略为 Python 字符上限。
        overlap: 最多补充的前文量；token 按 Token 计，其余策略按字符计。
        delimiters: delimiter 策略使用的字面分隔符序列，可包含一个或多个字符串。
        include_delimiter: 分隔符归入前块或后块，始终保留。
        recursive_delimiters: 递归策略的有序分隔符层级。
        sentence_delimiters: sentence/semantic 的句末规则。
        sentences_per_chunk: sentence 策略每片最多句子数。
        semantic_threshold: Chonkie 原生语义阈值。
        similarity_window: 原生语义比较的句子窗口。
        embedding_batch_size: 单次向量提供方请求上限。
        semantic_window_chars: 单次原生语义分析的字符上限。
        preserve_tables: 表格按行保持结构，默认启用。
        table_overflow: 无法容纳单行时明确报错，或显式允许并提示。
    """

    strategy: Literal["character", "recursive", "delimiter", "token", "sentence", "semantic"] = (
        "recursive"
    )
    size: int = Field(default=1000, ge=1, strict=True)
    overlap: int = Field(default=0, ge=0, strict=True)
    delimiters: tuple[str, ...] = ("\n\n", "\n")
    include_delimiter: Literal["previous", "next"] = "previous"
    recursive_delimiters: tuple[tuple[str, ...], ...] = _RECURSIVE_DELIMITERS
    sentence_delimiters: tuple[str, ...] = ("。", "！", "？", ". ", "! ", "? ", "\n")
    sentences_per_chunk: int = Field(default=1, ge=1, strict=True)
    semantic_threshold: float = Field(default=0.8, gt=0, lt=1, allow_inf_nan=False)
    similarity_window: int = Field(default=3, ge=1, strict=True)
    embedding_batch_size: int = Field(default=32, ge=1, strict=True)
    semantic_window_chars: int = Field(default=32000, ge=1, strict=True)
    preserve_tables: bool = True
    table_overflow: Literal["error", "allow"] = "error"

    # @field_validator("delimiters", mode="before", json_schema_input_type=str | tuple[str, ...])
    # @classmethod
    # def normalize_delimiters(cls, value: object) -> object:
    #     """将单个字符串作为一个完整的字面分隔符。"""
    #     return (value,) if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        """校验窗口进度和各策略使用的字面分隔符。

        Raises:
            ValueError: overlap 不小于 size，或分隔符为空、重复。
        """
        if self.overlap >= self.size:
            raise ValueError("overlap must be smaller than size")
        for level in (self.delimiters, self.sentence_delimiters, *self.recursive_delimiters):
            if not level or any(not item for item in level) or len(set(level)) != len(level):
                raise ValueError("delimiters must be nonempty and unique within each level")
        if not self.recursive_delimiters:
            raise ValueError("recursive delimiter levels must not be empty")
        return self


def _validated_policy(policy: ChunkPolicy) -> ChunkPolicy:
    """重新校验调用边界，防止 model_copy/model_construct 绕过配置校验。

    Args:
        policy: 调用方提供的策略对象。

    Returns:
        通过完整字段及跨字段校验的新策略对象。
    """
    return ChunkPolicy.model_validate(policy.model_dump())


def _fingerprint(
    policy: ChunkPolicy,
    ranges: Sequence[tuple[int, int]] | None = None,
    backend_identity: dict[str, str] | None = None,
) -> str:
    """计算配置摘要，纳入实现版本、实际规则和可选父块范围。

    Args:
        policy: 已校验的叶子块策略。
        ranges: 父子模式的原文范围；None 表示普通分块。
        backend_identity: Token 和向量提供方的显式版本身份。

    Returns:
        排序 JSON 的 SHA-256 十六进制摘要，不包含文档正文。
    """
    config = {
        "implementation": _IMPLEMENTATION_VERSION,
        "policy": policy.model_dump(),
        "recursive_delimiters": _RECURSIVE_DELIMITERS if policy.strategy == "recursive" else None,
        "parent_ranges": ranges,
        "backends": backend_identity or {},
    }
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def _chunk(
    document: Document,
    start: int,
    end: int,
    ordinal: int,
    fingerprint: str,
    content_digest: str,
    *,
    parent_id: str | None = None,
    ordinal_in_parent: int | None = None,
    kind: Literal["parent", "leaf"] = "leaf",
    mapper: SourceMapper | None = None,
    prefix: str = "",
    table: TableSlice | None = None,
    offset_base: int = 0,
) -> Chunk:
    """从原文绝对范围构造块，绑定内容版本并生成文本级来源。

    Args:
        document: 完整规范文本。
        start: 原文中的起点，包含此字符。
        end: 原文中的终点，不包含此字符。
        ordinal: 当前 kind 内的文档级顺序。
        fingerprint: 分块配置摘要。
        content_digest: 完整规范文本的 SHA-256，防止 revision 未更新时冲突。
        parent_id: 叶子块所属父块 ID。
        ordinal_in_parent: 子块在所属父块内从零开始的顺序。
        kind: parent 或 leaf。
        mapper: 可复用的全文来源索引。
        prefix: 表格后续分片重复的表头。
        table: 表格数据行范围与表头来源。
        offset_base: 流式窗口的原文起点，普通模式为零。

    Returns:
        正文严格取自 document.text[start:end] 的块。
    """
    identity = [
        document.id,
        document.revision_id,
        content_digest,
        start + offset_base,
        end + offset_base,
        kind,
        fingerprint,
        parent_id,
        prefix,
        None if table is None else table.model_dump(),
    ]
    return Chunk(
        id=hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
        document_id=document.id,
        revision_id=document.revision_id,
        text=document.text[start:end],
        start=start + offset_base,
        end=end + offset_base,
        ordinal=ordinal,
        kind=kind,
        parent_id=parent_id,
        ordinal_in_parent=ordinal_in_parent,
        context_prefix=prefix,
        table=table,
        source_refs=tuple(
            ref.model_copy(
                update={
                    "start": None if ref.start is None else ref.start + offset_base,
                    "end": None if ref.end is None else ref.end + offset_base,
                }
            )
            for ref in (mapper or SourceMapper(document)).resolve(start, end)
        ),
    )


def _source_warnings(document: Document) -> tuple[Issue, ...]:
    """原文件来源尚不能精确映射时给出可机器识别的提示。

    Args:
        document: 可能携带页码或元素引用的规范文档。

    Returns:
        有上游来源时提示当前只返回规范文本范围；无来源时为空元组。
    """
    if not document.source_refs or document.source_map:
        return ()
    return (
        Issue(
            code="source_mapping_unavailable",
            message=(
                "Chunk sources locate canonical text only; upstream file references are unmapped"
            ),
            input_id=document.id,
        ),
    )


def _recursive_spans(
    text: str,
    size: int,
    delimiters: tuple[tuple[str, ...], ...] = _RECURSIVE_DELIMITERS,
) -> list[tuple[int, int]]:
    """调用 Chonkie 并逐块验证位置、原文、上限和无遗漏覆盖。

    Args:
        text: 非空规范文本。
        size: 最大字符数。
        delimiters: 按优先级排列的字面分隔符层级。

    Returns:
        按原文排序、首尾衔接的非空区间。

    Raises:
        MissingDependencyError: 未安装 chunking extra。
        ChunkAlignmentError: 后端输出越界、丢字、重叠、错位或超过大小上限。
    """
    try:
        from chonkie import RecursiveChunker
        from chonkie.types import RecursiveLevel, RecursiveRules
    except ImportError as exc:
        raise MissingDependencyError("chunking") from exc
    rules = RecursiveRules(
        levels=[
            *(RecursiveLevel(delimiters=list(level)) for level in delimiters),
            RecursiveLevel(whitespace=True),
            RecursiveLevel(),
        ]
    )
    native = RecursiveChunker(
        tokenizer="character", chunk_size=size, rules=rules, min_characters_per_chunk=1
    )
    spans = []
    previous_end = 0
    for item in native.chunk(text):
        start, end = item.start_index, item.end_index
        if (
            type(start) is not int
            or type(end) is not int
            or start != previous_end
            or not 0 <= start < end <= len(text)
            or end - start > size
            or text[start:end] != item.text
        ):
            raise ChunkAlignmentError("Chonkie output violates text, size or contiguous offsets")
        spans.append((start, end))
        previous_end = end
    if previous_end != len(text):
        raise ChunkAlignmentError("Chonkie output does not cover the entire input")
    return spans


class Chunker:
    """将完整 Document 转换为叶子块，不修改原文或执行检索。

    递归、句子和语义能力使用 Chonkie；字符、字面分隔符及无损 Token 窗口由组件封装。
    向量和分词器显式注入；表格原子行及来源映射不依赖 Agent 或数据库。
    """

    def __init__(
        self,
        *,
        tokenizer: TokenEncoder | None = None,
        tokenizer_id: str | None = None,
        embedding: EmbeddingProvider | None = None,
        embedding_id: str | None = None,
    ) -> None:
        """注入已配置的分词器或向量能力，不隐式下载模型。

        Args:
            tokenizer: 实际 Token 编码器，不应自动添加 BOS/EOS。
            tokenizer_id: 包含版本/词表身份的稳定名称，注入 tokenizer 时必填。
            embedding: 固定维度的批量向量提供方。
            embedding_id: 包含模型版本和预处理身份的名称，注入 embedding 时必填。

        Raises:
            ValueError: 提供方未绑定显式身份或向量维度非法。
        """
        if (tokenizer is None) != (tokenizer_id is None) or tokenizer_id == "":
            raise ValueError("tokenizer and nonempty tokenizer_id must be configured together")
        if (embedding is None) != (embedding_id is None) or embedding_id == "":
            raise ValueError("embedding and nonempty embedding_id must be configured together")
        if embedding is not None and (
            type(embedding.dimension) is not int or embedding.dimension < 1
        ):
            raise ValueError("embedding dimension must be a positive integer")
        self._tokenizer, self._tokenizer_id = tokenizer, tokenizer_id
        self._embedding, self._embedding_id = embedding, embedding_id

    def configuration_fingerprint(
        self,
        policy: ChunkPolicy,
        ranges: Sequence[tuple[int, int]] | None = None,
    ) -> str:
        """计算包含真实后端身份的配置摘要。

        Args:
            policy: 已选择的分块配置。
            ranges: 父子模式的原文范围，普通模式省略。

        Returns:
            可用于记录和重复执行校验的配置摘要。

        Raises:
            ValueError: token 或 semantic 策略没有配置所需能力。
        """
        identity = {}
        if policy.strategy == "token":
            if self._tokenizer is None or self._tokenizer_id is None:
                raise ValueError("token strategy requires tokenizer and tokenizer_id")
            identity["tokenizer"] = self._tokenizer_id
        if policy.strategy == "semantic":
            if self._embedding is None or self._embedding_id is None:
                raise ValueError("semantic strategy requires embedding and embedding_id")
            identity["embedding"] = self._embedding_id
        return _fingerprint(policy, ranges, identity)

    def _measure(self, text: str, policy: ChunkPolicy) -> int:
        """按策略选择实际 Token 或字符计量。

        Args:
            text: 预算需包含重复表头时，调用方传入完整 index_text。
            policy: 确定计量单位的策略。

        Returns:
            实际编码长度或 Python 字符数。
        """
        if policy.strategy == "token":
            assert self._tokenizer is not None
            return token_count(self._tokenizer, text)
        return len(text)

    def _plain_spans(self, text: str, policy: ChunkPolicy) -> list[tuple[int, int]]:
        """调度六种普通文本策略并补充预算内前文。

        Args:
            text: 当前普通文本区间，不包含受保护的表格。
            policy: 已校验配置；重叠先预留预算，再从原文扩展起点。

        Returns:
            相对于当前 text 的有序区间，保留原文且不在表格边界外补前文。
        """
        if not text:
            return []
        size = policy.size - policy.overlap
        if policy.strategy == "character":
            base = [(i, min(i + size, len(text))) for i in range(0, len(text), size)]
            # 字符窗口维持精确步长，并让第一块利用完整预算。
            if policy.overlap:
                return [
                    (i, min(i + policy.size, len(text)))
                    for i in range(0, max(1, len(text) - policy.overlap), size)
                ]
        elif policy.strategy == "recursive":
            base = _recursive_spans(text, size, policy.recursive_delimiters)
        elif policy.strategy == "delimiter":
            raw = literal_spans(text, policy.delimiters, policy.include_delimiter)
            base = [(i, min(i + size, end)) for start, end in raw for i in range(start, end, size)]
        elif policy.strategy == "sentence":
            if text.isspace():
                base = [(i, min(i + size, len(text))) for i in range(0, len(text), size)]
            else:
                base = sentence_spans(
                    text, size, policy.sentence_delimiters, policy.sentences_per_chunk
                )
        elif policy.strategy == "token":
            assert self._tokenizer is not None
            base = token_spans(text, size, self._tokenizer)
        else:
            assert self._embedding is not None
            base = semantic_spans(
                text,
                size,
                self._embedding,
                threshold=policy.semantic_threshold,
                similarity_window=policy.similarity_window,
                delimiters=policy.sentence_delimiters,
                batch_size=policy.embedding_batch_size,
                window_chars=policy.semantic_window_chars,
            )
        if not policy.overlap:
            return base
        result = []
        for start, end in base:
            left = max(0, start - policy.overlap)
            if policy.strategy == "token":
                # Token 边界与字符边界不一致；只补充预算内的原文后缀并复验整片。
                left = start
                while left > 0 and self._measure(text[left - 1 : start], policy) <= policy.overlap:
                    if self._measure(text[left - 1 : end], policy) > policy.size:
                        break
                    left -= 1
            result.append((left, end))
        return result

    def _pieces(self, document: Document, policy: ChunkPolicy) -> list[SpanPiece]:
        """将原文按普通文本和受保护表格分区，再独立应用策略。

        Args:
            document: 完整原文或流式窗口。
            policy: 已校验的分块与表格规则。

        Returns:
            按原文顺序排列的正文区间、重复表头及表格行元数据。
        """
        tables = []
        if policy.preserve_tables:
            tables = [(e.start, e.end, e.id) for e in document.elements if e.kind == "table"]
            if not tables:
                tables = [
                    (a, b, f"markdown-table:{a}") for a, b in markdown_table_ranges(document.text)
                ]
        pieces: list[SpanPiece] = []
        position = 0
        for start, end, table_id in tables:
            pieces.extend(
                SpanPiece(position + a, position + b)
                for a, b in self._plain_spans(document.text[position:start], policy)
            )
            pieces.extend(
                split_table(
                    document.text,
                    start,
                    end,
                    table_id,
                    size=policy.size,
                    measure=(lambda value: self._measure(value, policy))
                    if policy.strategy == "token"
                    else len,
                    overflow=policy.table_overflow,
                )
            )
            position = end
        pieces.extend(
            SpanPiece(position + a, position + b)
            for a, b in self._plain_spans(document.text[position:], policy)
        )
        return pieces

    def chunk(self, document: Document, policy: ChunkPolicy | None = None) -> ChunkResult:
        """按六种策略切分全文，并保留结构、来源和实际覆盖。

        Args:
            document: 规范文本及可选 Docling 结构和 SourceMap。
            policy: 策略配置；默认递归、1000 字符、保留表格。

        Returns:
            有序叶子块、配置摘要、处理区间和显式超预算表格提示。

        Raises:
            ValueError: 配置、能力注入或表格硬预算不满足要求。
            MissingDependencyError: 所选原生策略缺少 Chonkie。
            ChunkAlignmentError: 原生输出不满足原文位置约束。
        """
        policy = _validated_policy(policy or ChunkPolicy())
        fingerprint = self.configuration_fingerprint(policy)
        mapper = SourceMapper(document)
        content_digest = hashlib.sha256(document.text.encode()).hexdigest()
        pieces = self._pieces(document, policy)
        chunks = tuple(
            _chunk(
                document,
                item.start,
                item.end,
                index,
                fingerprint,
                content_digest,
                mapper=mapper,
                prefix=item.prefix,
                table=item.table,
            )
            for index, item in enumerate(pieces)
        )
        warnings = _source_warnings(document) + tuple(
            Issue(
                code="oversized_table_row",
                message=(
                    "Table header plus an indivisible row exceeds size under explicit allow policy"
                ),
                input_id=item.id,
            )
            for item in chunks
            if item.table and item.table.oversized
        )
        return ChunkResult(
            chunks=chunks,
            config_fingerprint=fingerprint,
            input_length=len(document.text),
            covered_ranges=((0, len(document.text)),) if document.text else (),
            warnings=warnings,
        )


def _validated_ranges(
    ranges: Sequence[tuple[int, int]], length: int
) -> tuple[tuple[int, int], ...]:
    """在分块开始前完整验证父范围，允许有意跳过未选择的文本。

    Args:
        ranges: 已排序、不重叠的整数区间序列。
        length: 原文字符数。

    Returns:
        验证后的不可变范围快照。

    Raises:
        ValueError: 结构、整数类型、顺序、非空或边界约束不满足。
    """
    snapshot = tuple(ranges)
    previous_end = 0
    for interval in snapshot:
        if (
            not isinstance(interval, tuple | list)
            or len(interval) != 2
            or any(type(value) is not int for value in interval)
        ):
            raise ValueError("each parent range must contain two integer character offsets")
        start, end = interval
        if start < previous_end or not 0 <= start < end <= length:
            raise ValueError("parent ranges must be ordered, nonoverlapping and inside document")
        previous_end = end
    return tuple((start, end) for start, end in snapshot)


class ParentChildBuilder:
    """在调用方给定的父块范围内独立切子块，并转换为原文绝对偏移。

    build 接收显式范围，build_sections 委托结构分节组件。父块不受子策略大小限制。
    未被选入父范围的文本不会自动加入输出；实际范围随结果返回。
    """

    def __init__(self, chunker: Chunker | None = None) -> None:
        """绑定子块组件。

        Args:
            chunker: 可复用的分块器，默认使用本项目 Chunker。
        """
        self._chunker = chunker or Chunker()

    def build(
        self,
        document: Document,
        parent_ranges: Sequence[tuple[int, int]],
        child_policy: ChunkPolicy,
    ) -> ChunkResult:
        """先验证全部范围，再按父块顺序生成父块及其子块。

        Args:
            document: 完整规范文本。
            parent_ranges: 原文左闭右开范围；允许间隔或空序列，不允许重叠。
            child_policy: 每个父块内部独立执行的子块策略。

        Returns:
            parent、其 leaves、下一个 parent 的交错序列。ordinal 在 parent/leaf
            各自的文档序列中计数；ordinal_in_parent 为父块内的子块序号。

        Raises:
            ValueError: 策略或任一父范围无效，在调用子块器之前抛出。
            MissingDependencyError: 子块策略需要的后端未安装。
            ChunkAlignmentError: 子块后端输出不符合约束。
        """
        child_policy = _validated_policy(child_policy)
        ranges = _validated_ranges(parent_ranges, len(document.text))
        fingerprint = self._chunker.configuration_fingerprint(child_policy, ranges)
        mapper = SourceMapper(document)
        warnings = list(_source_warnings(document))
        content_digest = hashlib.sha256(document.text.encode()).hexdigest()
        all_chunks: list[Chunk] = []
        if child_policy.preserve_tables:
            protected = [(e.start, e.end) for e in document.elements if e.kind == "table"]
            if not protected:
                protected = markdown_table_ranges(document.text)
            protected_ends = tuple(right for _, right in protected)
            for start, end in ranges:
                for left, right in islice(protected, bisect_right(protected_ends, start), None):
                    if left >= end:
                        break
                    if not (start <= left and right <= end):
                        raise ValueError("parent range cuts through a preserved table")
        leaf_ordinal = 0
        element_ends = tuple(element.end for element in document.elements)
        for parent_ordinal, (start, end) in enumerate(ranges):
            parent = _chunk(
                document,
                start,
                end,
                parent_ordinal,
                fingerprint,
                content_digest,
                kind="parent",
                mapper=mapper,
            )
            all_chunks.append(parent)
            local_elements = []
            for element in islice(document.elements, bisect_right(element_ends, start), None):
                if element.start >= end:
                    break
                if (
                    child_policy.preserve_tables
                    and element.kind == "table"
                    and not (start <= element.start and element.end <= end)
                ):
                    raise ValueError("parent range cuts through a preserved table")
                if start <= element.start and element.end <= end:
                    local_elements.append(
                        element.model_copy(
                            update={"start": element.start - start, "end": element.end - start}
                        )
                    )
            subdocument = Document(
                id=document.id,
                revision_id=document.revision_id,
                text=document.text[start:end],
                elements=tuple(local_elements),
            )
            children = self._chunker.chunk(subdocument, child_policy)
            child_ids: dict[str, str] = {}
            for local_ordinal, child in enumerate(children.leaves):
                all_chunks.append(
                    _chunk(
                        document,
                        start + child.start,
                        start + child.end,
                        leaf_ordinal,
                        fingerprint,
                        content_digest,
                        parent_id=parent.id,
                        ordinal_in_parent=local_ordinal,
                        mapper=mapper,
                        prefix=child.context_prefix,
                        table=None
                        if child.table is None
                        else child.table.model_copy(
                            update={
                                "table_id": (
                                    f"markdown-table:{start + child.table.header_start}"
                                    if child.table.table_id.startswith("markdown-table:")
                                    else child.table.table_id
                                ),
                                "header_start": start + child.table.header_start,
                                "header_end": start + child.table.header_end,
                            }
                        ),
                    )
                )
                child_ids[child.id] = all_chunks[-1].id
                leaf_ordinal += 1
            warnings.extend(
                warning.model_copy(
                    update={"input_id": child_ids.get(str(warning.input_id), warning.input_id)}
                )
                for warning in children.warnings
            )
        return ChunkResult(
            chunks=tuple(all_chunks),
            config_fingerprint=fingerprint,
            input_length=len(document.text),
            covered_ranges=ranges,
            warnings=tuple(warnings),
        )

    def build_sections(
        self,
        document: Document,
        child_policy: ChunkPolicy,
        *,
        max_level: int = 6,
        include_preamble: bool = True,
    ) -> ChunkResult:
        """按原生 Docling 标题结构自动构建章节父子块。

        Args:
            document: 携带 Docling DocumentElement 的规范文档。
            child_policy: 每个章节内部执行的子策略。
            max_level: 纳入分节边界的最大标题层级。
            include_preamble: 是否保留首个标题之前的序言。

        Returns:
            章节父块及其子块；标题层级路径存于每块 section_path。
        """
        sections = SectionBuilder().build(
            document, max_level=max_level, include_preamble=include_preamble
        )
        result = self.build(
            document, [(section.start, section.end) for section in sections], child_policy
        )
        paths = {
            parent.id: section.path
            for parent, section in zip(result.parents, sections, strict=True)
        }
        return result.model_copy(
            update={
                "chunks": tuple(
                    chunk.model_copy(
                        update={
                            "section_path": paths[
                                chunk.id if chunk.kind == "parent" else str(chunk.parent_id)
                            ]
                        }
                    )
                    for chunk in result.chunks
                )
            }
        )
