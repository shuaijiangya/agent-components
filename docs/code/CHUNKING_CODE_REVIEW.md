# 分块相关代码检查稿

由 `scripts/export_chunking_review.py` 从真实源码生成。中文注释与代码保持原样，每节标明路径、SHA-256 和原文件行号。核心契约只节选分块相关类，其余模块完整展示；节选块不是独立可执行文件，运行请使用源码包。

功能与参数见 [CHUNKING.md](../CHUNKING.md)，行为、代码与测试的对应关系见 [CHUNKING_REVIEW.md](../reviews/CHUNKING_REVIEW.md)。

| 文件 | SHA-256 |
| --- | --- |
| `src/agent_components/core/contracts.py` | `d556b77209df4794a0977b77fc63ef157d29303d89171406a8f839c9ef22d2b8` |
| `src/agent_components/core/errors.py` | `a2254fcc0d91555952b1f44f86831ae3cde1b6593a31f5e498e04ab48c9cbbab` |
| `src/agent_components/chunking/__init__.py` | `0789bc51387de619a2ee6fd42fc0d62f34a248f2b7520edc107d77d8279f8649` |
| `src/agent_components/chunking/chunker.py` | `324a537283f768081c8d2e518bc1555bd2e0db090665176dca96730b33e574af` |
| `src/agent_components/chunking/backends.py` | `2ebc46e927f5b22bddc2ac6cb026ea8c7eb5e264fcba9db2907211384313f68e` |
| `src/agent_components/chunking/sections.py` | `f895e39afbc740059268726131fe3cbdfe7ff84d2fed20eb8a5c29c95f8f4313` |
| `src/agent_components/chunking/rechunk.py` | `a7d95767ca3c03099f512f377adf559dd48e5aec06035af81c3a23dc55e3ed8b` |
| `src/agent_components/core/markdown_tables.py` | `5d5b541c2b177c08aa1ec29df3949a5124101fae28cb1a4ce622afd540d1c2cc` |
| `src/agent_components/chunking/tables.py` | `4b4538b19844025c52094c1bfc211455d3d87f434cd7a24983c7397f1bd81fe7` |
| `src/agent_components/chunking/sources.py` | `c90b4cfcf42ec7d0610d94fa4a2f030a2d1965c3c96fa75d0edbf2774dbf1e0e` |
| `src/agent_components/parsing/__init__.py` | `7ea81934a00b3777b7bb827eb8dc78b599e2a664f3fbf5ca7433119880124190` |
| `src/agent_components/parsing/docling.py` | `f28a7d3daf4d717dd564eb56101ba68d84a48dd34a0302be39acff567731d4f4` |
| `src/agent_components/chunking/streaming.py` | `22aa613a2391cdb6b750c90b87a4208f8ff9b82f9148b9e02af08b3fd4ed9deb` |
| `src/agent_components/retrieval/context.py` | `32e5a77f0d9b3a69ed243ca44639bb3c049948364935fc4a91cbb6a88f8adba2` |
| `src/agent_components/retrieval/selection.py` | `c54d25426473958f15ec355d6693f068267527c3a6e15e002d1995b312935e8c` |

## src/agent_components/core/contracts.py

[打开完整源码](../../src/agent_components/core/contracts.py)

### Contract（原文件第 11 行）

````python
class Contract(BaseModel):
    """所有公共数据对象的基础约束。

    禁止未知字段及属性赋值；需要修改时显式构造新对象并重新校验。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
````

### SourceRef（原文件第 20 行）

````python
class SourceRef(Contract):
    """描述输入文本或原文件的来源位置。

    start/end 必须同时存在或同时省略，采用 Python 字符的左闭右开范围。
    page 从一开始；precision 表明实际定位精度，不能凭文本偏移推断页码。
    bbox 为原生元素的 (left, top, right, bottom)，coord_origin 表明坐标原点；
    两者不随文本截取插值缩小，元素引用不等于逐字符页面坐标。
    """

    input_id: str
    document_id: str | None = None
    revision_id: str | None = None
    element_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    precision: Literal["text", "element", "page", "unknown"] = "text"
    bbox: tuple[float, float, float, float] | None = None
    coord_origin: str | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        """验证来源区间的成对性和顺序。

        Raises:
            ValueError: 端点不成对或 end 小于 start。
        """
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must both be set or both be absent")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("end must be greater than or equal to start")
        return self
````

### SourceSpan（原文件第 54 行）

````python
class SourceSpan(Contract):
    """将规范文本区间映射到原生元素来源。

    起止位置属于 Document.text；refs 的元素、页码和矩形来自后端，
    不将 Markdown 字符距离线性换算为原文件字符或页面坐标。
    """

    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    refs: tuple[SourceRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        """拒绝空区间或反向区间。

        Raises:
            ValueError: end 不大于 start。
        """
        if self.end <= self.start:
            raise ValueError("source span must be nonempty")
        return self
````

### DocumentElement（原文件第 77 行）

````python
class DocumentElement(Contract):
    """表示 Docling 阅读顺序中的结构元素。

    heading 的 level 来自原生标题层级；table 范围只涵盖表格本体，
    不通过标题正则或首次字符串查找猜测结构位置。
    """

    id: str
    kind: Literal["text", "heading", "table", "other"]
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    level: int | None = Field(default=None, ge=1, strict=True)
    title: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        """验证元素范围及标题层级。

        Raises:
            ValueError: 范围为空或 heading 缺少 level。
        """
        if self.end <= self.start or (self.kind == "heading" and self.level is None):
            raise ValueError("invalid element range or missing heading level")
        return self
````

### Document（原文件第 103 行）

````python
class Document(Contract):
    """承载解析后的完整规范文本。

    Attributes:
        id: 使用方定义的文档身份，应在所属知识库范围内唯一。
        text: 分块、抽取和文本偏移共同使用的原文基准。
        revision_id: 使用方提供的文档版本；默认值不代表自动版本管理。
        metadata: 业务标签或解析元信息，不参与模型提示的自动拼接。
        source_refs: 无法展开到文本区间的上游来源提示。
        source_map: 有序文本区间到原生元素、页码和矩形的映射。
        elements: 原生标题、表格和文本元素，不根据正文猜测标题。
    """

    id: str = Field(min_length=1)
    text: str
    revision_id: str = "1"
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    source_refs: tuple[SourceRef, ...] = ()
    source_map: tuple[SourceSpan, ...] = ()
    elements: tuple[DocumentElement, ...] = ()

    @model_validator(mode="after")
    def validate_structure(self) -> Self:
        """检查映射与结构范围有序、不重叠并处于规范文本内。

        Raises:
            ValueError: 任一范围越界、反向或与前一区间重叠。
        """
        for ranges in (self.source_map, self.elements):
            previous = 0
            for item in ranges:
                if item.start < previous or item.end > len(self.text):
                    raise ValueError("document ranges must be ordered and inside text")
                previous = item.end
        return self
````

### TableSlice（原文件第 140 行）

````python
class TableSlice(Contract):
    """记录表格分片的原始行范围和重复表头来源。

    row_start/row_end 是零起点数据行区间；header_start/header_end 指向原文表头。
    重复表头存于 Chunk.context_prefix，不改变正文的原文偏移。
    """

    table_id: str
    row_start: int = Field(ge=0)
    row_end: int = Field(ge=0)
    header_start: int = Field(ge=0)
    header_end: int = Field(ge=0)
    oversized: bool = False
````

### Chunk（原文件第 155 行）

````python
class Chunk(Contract):
    """承载一个父块或叶子块及其原文位置。

    Attributes:
        id: 组件生成的确定性身份，包含内容和配置摘要。
        document_id: 原文档身份。
        revision_id: 原文档版本。
        text: 原文中的连续片段，不自动去除空白。
        start: 片段在完整规范文本中的包含端点。
        end: 片段在完整规范文本中的排除端点。
        ordinal: 当前 kind 内从零开始的文档级顺序。
        kind: parent 为上下文范围，leaf 为实际切分的子块。
        parent_id: 所属父块；普通分块为空。
        ordinal_in_parent: 所属父块内的子块顺序；普通块和父块为空。
        source_refs: 精确规范文本范围及 SourceMap 能提供的原生元素来源。
        section_path: 自动章节模式保留的标题层级路径。
        context_prefix: 表格后续片重复的表头，与原文正文分开。
        table: 表格数据行区间及表头来源。
        index_text: 只读属性，返回 context_prefix 加 text。
    """

    id: str = Field(min_length=1)
    document_id: str
    revision_id: str
    text: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    ordinal: int = Field(ge=0)
    kind: Literal["parent", "leaf"] = "leaf"
    parent_id: str | None = None
    ordinal_in_parent: int | None = Field(default=None, ge=0, strict=True)
    section_path: tuple[str, ...] = ()
    context_prefix: str = ""
    table: TableSlice | None = None
    source_refs: tuple[SourceRef, ...] = ()

    @property
    def index_text(self) -> str:
        """读取供索引或模型使用的表头加正文。

        Returns:
            重复上下文前缀与原文正文的组合；普通块等于 text。
        """
        return self.context_prefix + self.text

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        """验证文本长度和左闭右开区间一致。

        Raises:
            ValueError: 区间反向或字符数与正文不符。
        """
        if self.end < self.start or self.end - self.start != len(self.text):
            raise ValueError("chunk offsets must match its text length (Python character indices)")
        return self
````

### Issue（原文件第 212 行）

````python
class Issue(Contract):
    """携带可机器识别的错误或提示。

    code 用于程序分支，message 用于解释；path 为 JSON Pointer，input_id 指向来源。
    """

    code: str
    message: str
    path: str = ""
    input_id: str | None = None
````

### ChunkResult（原文件第 224 行）

````python
class ChunkResult(Contract):
    """记录分块产物、实际处理范围和来源提示。

    Attributes:
        chunks: 普通模式为叶子序列；父子模式为父块及其子块的交错序列。
        config_fingerprint: 实现版本、配置、规则及父范围的摘要，不是正文摘要。
        input_length: 完整规范文本的 Python 字符数。
        covered_ranges: 实际处理的原文区间；父子模式保留所选父范围，不自动补齐间隙。
        warnings: 来源映射等非致命限制；调用方需要显式处理。
    """

    chunks: tuple[Chunk, ...]
    config_fingerprint: str
    input_length: int = Field(ge=0, strict=True)
    covered_ranges: tuple[tuple[int, int], ...]
    warnings: tuple[Issue, ...] = ()

    @property
    def parents(self) -> tuple[Chunk, ...]:
        """按输出顺序读取父块。

        Returns:
            kind 为 parent 的块；普通分块结果为空元组。
        """
        return tuple(item for item in self.chunks if item.kind == "parent")

    @property
    def leaves(self) -> tuple[Chunk, ...]:
        """按文档顺序读取叶子块。

        Returns:
            可供索引的 leaf 块，排除父块以避免重复索引上下文。
        """
        return tuple(item for item in self.chunks if item.kind == "leaf")
````

### ParseProvenance（原文件第 260 行）

````python
class ParseProvenance(Contract):
    """记录解析产物的来源摘要、后端和已知解析配置。

    外部原生对象或历史产物可能缺少配置和源文件摘要，缺失值表示未知。
    """

    backend: str = Field(min_length=1)
    backend_version: str | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    options: dict[str, JsonValue] | None = None
````

### ParsedDocument（原文件第 272 行）

````python
class ParsedDocument(Contract):
    """记录规范文本、Markdown 和后端原生结构。

    source_mapping 为 text_only 时不能把规范文本偏移解释为文件页码或元素坐标。
    """

    document: Document
    markdown: str
    native_json: dict[str, JsonValue]
    source_mapping: Literal["text_only", "mapped"] = "text_only"
    provenance: ParseProvenance | None = None
````


## src/agent_components/core/errors.py

[打开完整源码](../../src/agent_components/core/errors.py)

````python
"""定义组件调用方可捕获的稳定异常类型。

code 提供跨 Adapter 的错误分类，底层异常可通过异常链定位。
"""


class ComponentError(Exception):
    """作为所有组件分类异常的基类。

    code 是程序可识别的分类，不包含业务重试或审批决策。
    """

    code = "component_error"


class ChunkAlignmentError(ComponentError):
    """分块后端违反位置、内容、大小或完整覆盖约束。

    调用方应停止发布本批分块；组件不会用模糊查找猜测原文位置。
    """

    code = "chunk_alignment_error"


class MissingDependencyError(ComponentError):
    """表示调用的可选能力尚未安装。

    错误消息给出需安装的 extras，不在运行中自动安装软件包。
    """

    code = "missing_dependency"

    def __init__(self, extra: str) -> None:
        """生成明确的可选依赖安装提示。

        Args:
            extra: pyproject.toml 中的可选依赖组名称。
        """
        super().__init__(f"Install enterprise-agent-components[{extra}] to use this backend")


class UnsupportedCapabilityError(ComponentError):
    """表示当前 Adapter 或配置不支持所请求的能力。

    调用方可以选择其他后端，不应把缺少能力当作成功。
    """

    code = "unsupported_capability"


class InvalidSchemaError(ComponentError):
    """表示目标 JSON Schema 本身无效。

    应修正输入契约后再调用模型，而非重试相同请求。
    """

    code = "invalid_schema"


class RoutingError(ComponentError):
    """表示模型别名或路由配置无效。

    在请求发往供应商之前抛出。
    """

    code = "routing_error"


class RevisionConflictError(ComponentError):
    """表示持久化内容或状态的版本条件不再满足。

    调用方应重新读取状态并决定如何处理冲突，不自动覆盖。
    """

    code = "revision_conflict"


class RetryableError(ComponentError):
    """显式标记允许有限重试的瞬时失败。

    业务写操作是否可重试由调用方负责分类，执行器不会自行推断幂等性。
    """

    code = "retryable_error"
````

## src/agent_components/chunking/__init__.py

[打开完整源码](../../src/agent_components/chunking/__init__.py)

````python
"""提供普通分块、父子分块和流式分块入口。"""

from agent_components.chunking.backends import EmbeddingProvider, TokenEncoder
from agent_components.chunking.chunker import Chunker, ChunkPolicy, ParentChildBuilder
from agent_components.chunking.rechunk import RechunkedDocument, Rechunker, RechunkTrace
from agent_components.chunking.sections import Section, SectionBuilder, SectionOptions
from agent_components.chunking.sources import SourceMapper
from agent_components.chunking.streaming import StreamingChunker

__all__ = [
    "ChunkPolicy",
    "Chunker",
    "EmbeddingProvider",
    "ParentChildBuilder",
    "RechunkedDocument",
    "Rechunker",
    "RechunkTrace",
    "Section",
    "SectionBuilder",
    "SectionOptions",
    "SourceMapper",
    "StreamingChunker",
    "TokenEncoder",
]
````

## src/agent_components/chunking/chunker.py

[打开完整源码](../../src/agent_components/chunking/chunker.py)

````python
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
from agent_components.chunking.sections import (
    _SECTION_LAYOUT_VERSION,
    DEFAULT_MAX_PARENT_CHARS,
    SectionBuilder,
    SectionOptions,
    _bounded_parents,
)
from agent_components.chunking.sources import SourceMapper
from agent_components.chunking.tables import SpanPiece, split_table
from agent_components.core.contracts import (
    Chunk,
    ChunkResult,
    Contract,
    Document,
    DocumentElement,
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
    section_path: tuple[str, ...] = (),
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
        section_path: 自动章节父块及其子块的标题路径。
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
        section_path=section_path,
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


def _table_ranges(
    document: Document, preserve: bool, *, include_markdown_gaps: bool = False
) -> list[tuple[int, int, str]]:
    if not preserve:
        return []
    tables = [(e.start, e.end, e.id) for e in document.elements if e.kind == "table"]
    if tables and not include_markdown_gaps:
        return tables
    # 父块规划保留原生表格，并补充不与原生表格相交的 GFM 表格。
    # 在全文中扫描，避免子范围裁掉 Markdown 围栏后把代码误认成表格。
    native_ends = tuple(end for _, end, _ in tables)
    markdown = []
    for start, end in markdown_table_ranges(document.text):
        index = bisect_right(native_ends, start)
        if index == len(tables) or tables[index][0] >= end:
            markdown.append((start, end, f"markdown-table:{start}"))
    return sorted([*tables, *markdown])


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
        return _fingerprint(policy, ranges, self._backend_identity(policy))

    def _backend_identity(self, policy: ChunkPolicy) -> dict[str, str]:
        """读取实际启用的分词器或向量提供方身份。"""
        identity = {}
        if policy.strategy == "token":
            if self._tokenizer is None or self._tokenizer_id is None:
                raise ValueError("token strategy requires tokenizer and tokenizer_id")
            identity["tokenizer"] = self._tokenizer_id
        if policy.strategy == "semantic":
            if self._embedding is None or self._embedding_id is None:
                raise ValueError("semantic strategy requires embedding and embedding_id")
            identity["embedding"] = self._embedding_id
        return identity

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
        tables = _table_ranges(document, policy.preserve_tables)
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

    build 接收显式范围，build_sections 按独立字符预算生成父块。
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
        tables = _table_ranges(document, child_policy.preserve_tables, include_markdown_gaps=True)
        return self._build(
            document, [(SpanPiece(start, end), ()) for start, end in ranges], child_policy, tables
        )

    def _build(
        self,
        document: Document,
        parents: Sequence[tuple[SpanPiece, tuple[str, ...]]],
        child_policy: ChunkPolicy,
        tables: Sequence[tuple[int, int, str]],
        *,
        section_options: SectionOptions | None = None,
    ) -> ChunkResult:
        ranges = tuple((piece.start, piece.end) for piece, _ in parents)
        fingerprint = self._chunker.configuration_fingerprint(child_policy, ranges)
        if section_options is not None:
            identity = [fingerprint, _SECTION_LAYOUT_VERSION, section_options.model_dump()]
            fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        mapper = SourceMapper(document)
        warnings = list(_source_warnings(document))
        content_digest = hashlib.sha256(document.text.encode()).hexdigest()
        all_chunks: list[Chunk] = []
        table_ends = tuple(end for _, end, _ in tables)
        # 在任何子块工作前锁定全局识别到的表格，避免子范围丢失 Markdown 围栏状态。
        local_tables = []
        for piece, _ in parents:
            structures = []
            if piece.table is not None:
                structures.append(
                    DocumentElement(
                        id=piece.table.table_id,
                        kind="table",
                        start=0,
                        end=piece.end - piece.start + len(piece.prefix),
                    )
                )
            else:
                for left, right, table_id in islice(
                    tables, bisect_right(table_ends, piece.start), None
                ):
                    if left >= piece.end:
                        break
                    if not (piece.start <= left and right <= piece.end):
                        raise ValueError("parent range cuts through a preserved table")
                    structures.append(
                        DocumentElement(
                            id=table_id,
                            kind="table",
                            start=left - piece.start,
                            end=right - piece.start,
                        )
                    )
            local_tables.append(tuple(structures))
        leaf_ordinal = 0
        for parent_ordinal, ((piece, path), local_elements) in enumerate(
            zip(parents, local_tables, strict=True)
        ):
            start, end = piece.start, piece.end
            parent = _chunk(
                document,
                start,
                end,
                parent_ordinal,
                fingerprint,
                content_digest,
                kind="parent",
                section_path=path,
                mapper=mapper,
                prefix=piece.prefix,
                table=piece.table,
            )
            all_chunks.append(parent)
            subdocument = Document(
                id=document.id,
                revision_id=document.revision_id,
                text=piece.prefix + document.text[start:end],
                elements=local_elements,
            )
            local_policy = (
                child_policy
                if local_elements
                else child_policy.model_copy(update={"preserve_tables": False})
            )
            children = self._chunker.chunk(subdocument, local_policy)
            child_ids: dict[str, str] = {}
            prefix_length = len(piece.prefix)
            for local_ordinal, child in enumerate(children.leaves):
                table = child.table
                if table is not None and piece.table is not None:
                    # 临时表头只用于预算。真实表头位置及数据行序号沿用父片的全局信息。
                    table = piece.table.model_copy(
                        update={
                            "row_start": piece.table.row_start + table.row_start,
                            "row_end": piece.table.row_start + table.row_end,
                            "oversized": table.oversized,
                        }
                    )
                elif table is not None:
                    table = table.model_copy(
                        update={
                            "header_start": start + table.header_start,
                            "header_end": start + table.header_end,
                        }
                    )
                all_chunks.append(
                    _chunk(
                        document,
                        start + max(0, child.start - prefix_length),
                        start + child.end - prefix_length,
                        leaf_ordinal,
                        fingerprint,
                        content_digest,
                        parent_id=parent.id,
                        ordinal_in_parent=local_ordinal,
                        section_path=path,
                        mapper=mapper,
                        prefix=child.context_prefix or piece.prefix,
                        table=table,
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
        max_parent_chars: int = DEFAULT_MAX_PARENT_CHARS,
    ) -> ChunkResult:
        """按原生 Docling 标题结构自动构建章节父子块。

        Args:
            document: 携带 Docling DocumentElement 的规范文档。
            child_policy: 每个章节内部执行的子策略。
            max_level: 纳入分节边界的最大标题层级。
            include_preamble: 是否保留首个标题之前的序言。
            max_parent_chars: 父块字符上限，包含重复表头，与子策略预算独立。

        Returns:
            章节父块及其子块；标题层级路径存于每块 section_path。
        """
        options = SectionOptions(
            max_level=max_level,
            include_preamble=include_preamble,
            max_parent_chars=max_parent_chars,
        )
        child_policy = _validated_policy(child_policy)
        sections = SectionBuilder().build(
            document, max_level=options.max_level, include_preamble=options.include_preamble
        )
        tables = _table_ranges(document, child_policy.preserve_tables, include_markdown_gaps=True)
        parents = _bounded_parents(document, sections, tables, options.max_parent_chars)
        return self._build(document, parents, child_policy, tables, section_options=options)
````

## src/agent_components/chunking/backends.py

[打开完整源码](../../src/agent_components/chunking/backends.py)

````python
"""实现可注入的 Token 计量、句子边界和原生 Chonkie 语义策略。

模型与分词器由使用方提供；不根据模型名隐式下载权重，也不把字符数冒充 Token。
"""

import math
import re
from collections.abc import Sequence
from typing import Any, Protocol

from agent_components.core.errors import ChunkAlignmentError, MissingDependencyError


class TokenEncoder(Protocol):
    """约定真实 Token 编码器的最小能力。

    可直接传入已加载的 tiktoken、Hugging Face tokenizer 或兼容对象。
    """

    def encode(self, text: str) -> Sequence[int]:
        """编码文本，不自动加入特殊 Token。

        Args:
            text: 待编码的原文。

        Returns:
            实际 Token ID 序列。
        """
        ...


class EmbeddingProvider(Protocol):
    """约定由调用方配置的批量向量能力。

    dimension 必须固定；输出必须是非零、有限且维度一致的向量。
    """

    dimension: int

    def embed_batch(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """批量计算文本向量。

        Args:
            texts: 原文片段的有序序列。

        Returns:
            与输入一一对应的向量。
        """
        ...


def literal_spans(text: str, delimiters: Sequence[str], include: str) -> list[tuple[int, int]]:
    """保留字面分隔符并返回原文区间。

    Args:
        text: 完整待切文本。
        delimiters: 非空字面量序列，不执行调用方的正则。
        include: previous 归前块，next 归后块。

    Returns:
        无丢失、无重叠的非空范围。
    """
    pattern = re.compile("|".join(re.escape(d) for d in sorted(delimiters, key=len, reverse=True)))
    boundaries = [0]
    for match in pattern.finditer(text):
        point = match.end() if include == "previous" else match.start()
        if point > boundaries[-1]:
            boundaries.append(point)
    if boundaries[-1] != len(text):
        boundaries.append(len(text))
    return list(zip(boundaries[:-1], boundaries[1:], strict=True))


def token_count(encoder: TokenEncoder, text: str) -> int:
    """读取实际 Token 数并拒绝非空文本的空编码。

    Args:
        encoder: 使用方注入的编码器。
        text: 原文片段。

    Returns:
        实际编码长度。

    Raises:
        ValueError: 非空文本被编码为空。
    """
    count = len(encoder.encode(text))
    if text and count == 0:
        raise ValueError("tokenizer produced no tokens for nonempty text")
    return count


def token_spans(text: str, size: int, encoder: TokenEncoder) -> list[tuple[int, int]]:
    """在原文字符边界寻找满足真实 Token 预算的窗口。

    Args:
        text: 待切文本。
        size: 每片实际 Token 上限。
        encoder: 已配置的真实编码器。

    Returns:
        可完整还原原文的区间；不解码截断 Token，避免生成替换字符。

    Raises:
        ValueError: 当前预算无法容纳待切的单个 Python 字符。
    """
    result = []
    start = 0
    while start < len(text):
        # 仅探测有界字符窗口；BPE 的计数不严格单调，最终总是复验所选切片。
        high = min(len(text), start + max(64, size * 16))
        if token_count(encoder, text[start:high]) <= size:
            end = high
        else:
            low, upper = start + 1, high
            end = start
            while low <= upper:
                mid = (low + upper) // 2
                if token_count(encoder, text[start:mid]) <= size:
                    end, low = mid, mid + 1
                else:
                    upper = mid - 1
            if end == start:
                raise ValueError("token budget cannot fit a character; increase size")
        result.append((start, end))
        start = end
    return result


def sentence_spans(
    text: str,
    size: int,
    delimiters: Sequence[str],
    sentences_per_chunk: int,
) -> list[tuple[int, int]]:
    """使用 Chonkie 的句子边界逻辑并限制每块句子数量。

    Args:
        text: 待切原文。
        size: 硬字符上限，超长单句会继续切分。
        delimiters: 句末字面分隔符。
        sentences_per_chunk: 每块最多句子数。

    Returns:
        连续、保留空白的原文区间。

    Raises:
        MissingDependencyError: 未安装 chunking extra。
        ChunkAlignmentError: 原生句子切分未完整保留原文。
    """
    try:
        from chonkie.chunker.base import split_text_by_delimiters
    except ImportError as exc:
        raise MissingDependencyError("chunking") from exc
    sentences = split_text_by_delimiters(
        text, delimiters=list(delimiters), include_delim="prev", min_chars=1
    )
    if "".join(sentences) != text:
        raise ChunkAlignmentError("sentence splitting did not preserve original text")
    spans = []
    position = 0
    for offset in range(0, len(sentences), sentences_per_chunk):
        length = sum(len(s) for s in sentences[offset : offset + sentences_per_chunk])
        for start in range(position, position + length, size):
            spans.append((start, min(start + size, position + length)))
        position += length
    return spans


def semantic_spans(
    text: str,
    size: int,
    embedding: EmbeddingProvider,
    *,
    threshold: float,
    similarity_window: int,
    delimiters: Sequence[str],
    batch_size: int,
    window_chars: int,
) -> list[tuple[int, int]]:
    """桥接注入向量到 Chonkie SemanticChunker，并复验连续原文。

    Args:
        text: 待切原文。
        size: 最终字符上限。
        embedding: 调用方提供的向量能力。
        threshold: 原生语义分界阈值。
        similarity_window: 原生前文比较窗口。
        delimiters: 句末边界。
        batch_size: 每次向量请求的最大文本数。
        window_chars: 单次语义分析最多处理的原文字符数。

    Returns:
        语义分组的原文范围，超长组按字符上限继续拆分。

    Raises:
        MissingDependencyError: 未安装 chunking extra。
        ChunkAlignmentError: 原生分组无法逐段还原原文。
        ValueError: 向量数目、维度或有限性不符合契约。
    """
    try:
        import numpy as np
        from chonkie import SemanticChunker
        from chonkie.embeddings import BaseEmbeddings
    except ImportError as exc:
        raise MissingDependencyError("chunking") from exc

    class Bridge(BaseEmbeddings):  # type: ignore[misc]
        """把显式注入的批量向量能力转换为 Chonkie 原生接口。

        只使用已提供对象，不调用 AutoEmbeddings 或任何模型下载入口。
        """

        @property
        def dimension(self) -> int:
            """读取注入提供方的固定维度。

            Returns:
                向量维度。
            """
            return embedding.dimension

        def get_tokenizer(self) -> str:
            """使原生语义算法使用字符预算。

            Returns:
                原生 character tokenizer 名称。
            """
            return "character"

        def embed(self, text: str) -> Any:
            """计算单条向量。

            Args:
                text: 原生算法需要比较的文本。

            Returns:
                经校验的 NumPy 向量。
            """
            return self.embed_batch([text])[0]

        def embed_batch(self, texts: list[str]) -> list[Any]:
            """以有界批次调用提供方并校验向量。

            Args:
                texts: 原生算法生成的窗口或句子。

            Returns:
                有限、非零、维度一致的 NumPy 向量。
            """
            output = []
            for offset in range(0, len(texts), batch_size):
                batch = texts[offset : offset + batch_size]
                vectors = list(embedding.embed_batch(batch))
                if len(vectors) != len(batch):
                    raise ValueError("embedding result count differs from input")
                for vector in vectors:
                    if (
                        len(vector) != embedding.dimension
                        or len(vector) == 0
                        or not all(math.isfinite(v) for v in vector)
                        or not any(v != 0 for v in vector)
                    ):
                        raise ValueError("invalid semantic embedding vector")
                    output.append(np.asarray(vector, dtype=float))
            return output

    native = SemanticChunker(
        embedding_model=Bridge(),
        threshold=threshold,
        chunk_size=size,
        similarity_window=similarity_window,
        min_characters_per_sentence=1,
        delim=list(delimiters),
        skip_window=0,
        filter_window=5,
        filter_polyorder=3,
    )
    spans = []
    for origin in range(0, len(text), window_chars):
        window = text[origin : origin + window_chars]
        if window.isspace():
            chunks = [window]
        else:
            chunks = [item.text for item in native.chunk(window)]
        if "".join(chunks) != window:
            raise ChunkAlignmentError("semantic grouping did not preserve original text")
        position = origin
        for value in chunks:
            for start in range(position, position + len(value), size):
                spans.append((start, min(start + size, position + len(value))))
            position += len(value)
    return spans
````

## src/agent_components/chunking/sections.py

[打开完整源码](../../src/agent_components/chunking/sections.py)

````python
"""从 Docling 原生标题层级生成不重叠的章节范围。

不通过 Markdown 的井号或文本相似度推测标题，保留完整层级路径。
"""

from bisect import bisect_right
from collections.abc import Iterator, Sequence

from pydantic import Field

from agent_components.chunking.tables import SpanPiece, split_table
from agent_components.core.contracts import Contract, Document

DEFAULT_MAX_PARENT_CHARS = 4000
_SECTION_LAYOUT_VERSION = "bounded-sections-v1"


class SectionOptions(Contract):
    """配置章节识别和自动父块的字符预算。

    max_parent_chars 包含表格的重复表头，与子块字符或 Token 预算独立。
    """

    max_level: int = Field(default=6, ge=1, strict=True)
    include_preamble: bool = True
    max_parent_chars: int = Field(default=DEFAULT_MAX_PARENT_CHARS, ge=1, strict=True)


class Section(Contract):
    """描述一个分节的原文范围和标题路径。

    level 为零时表示没有标题的全文或序言。
    """

    start: int
    end: int
    level: int
    path: tuple[str, ...]


class SectionBuilder:
    """把指定标题深度以内的边界平铺为章节父范围。

    父子块采用两层结构；标题祖先关系通过 path 保留，不生成相互重叠的父范围。
    """

    def build(
        self,
        document: Document,
        *,
        max_level: int = 6,
        include_preamble: bool = True,
    ) -> tuple[Section, ...]:
        """根据结构标题生成连续分节。

        Args:
            document: Docling 解析产物或显式提供结构的规范文档。
            max_level: 作为边界的最大标题层级，必须为正整数。
            include_preamble: 是否保留第一个标题之前的文本。

        Returns:
            按原文顺序的章节；没有标题时将全文视为一节，空文本返回空元组。

        Raises:
            ValueError: max_level 非正整数。
        """
        if type(max_level) is not int or max_level < 1:
            raise ValueError("max_level must be a positive integer")
        if not document.text:
            return ()
        headings = [
            e
            for e in document.elements
            if e.kind == "heading" and e.level is not None and e.level <= max_level
        ]
        if not headings:
            return (Section(start=0, end=len(document.text), level=0, path=()),)
        sections = []
        if include_preamble and headings[0].start:
            sections.append(Section(start=0, end=headings[0].start, level=0, path=()))
        stack: list[tuple[int, str]] = []
        for index, heading in enumerate(headings):
            assert heading.level is not None
            while stack and stack[-1][0] >= heading.level:
                stack.pop()
            stack.append((heading.level, heading.title or ""))
            end = headings[index + 1].start if index + 1 < len(headings) else len(document.text)
            sections.append(
                Section(
                    start=heading.start,
                    end=end,
                    level=heading.level,
                    path=tuple(title for _, title in stack),
                )
            )
        return tuple(sections)


def _body_pieces(text: str, start: int, end: int, limit: int) -> Iterator[SpanPiece]:
    """在窗口后半段优先使用段落、行或句末边界，否则按字符上限切开。"""
    while start < end:
        stop = min(start + limit, end)
        if stop < end:
            floor = start + limit // 2
            for delimiters in (("\n\n",), ("\n",), ("。", "！", "？", ". ", "! ", "? ")):
                candidates = [(text.rfind(d, floor, stop), len(d)) for d in delimiters]
                boundary = max((pos + size for pos, size in candidates if pos >= 0), default=0)
                if boundary:
                    stop = boundary
                    break
        yield SpanPiece(start, stop)
        start = stop


def _bounded_parents(
    document: Document,
    sections: Sequence[Section],
    tables: Sequence[tuple[int, int, str]],
    limit: int,
) -> tuple[tuple[SpanPiece, tuple[str, ...]], ...]:
    """为章节父块生成有界范围，长表格按完整行分片。

    章节路径沿用原标题。表格父片的正文取原文，后续片另携带重复表头。
    若一行加表头仍超过父预算，抛出错误，避免悄悄产生超预算的父块。
    """
    result = []
    table_ends = tuple(end for _, end, _ in tables)
    for section in sections:
        section_tables = []
        for index in range(bisect_right(table_ends, section.start), len(tables)):
            start, end, table_id = tables[index]
            if start >= section.end:
                break
            if not section.start <= start < end <= section.end:
                raise ValueError("section boundary cuts through a preserved table")
            section_tables.append((start, end, table_id))
        if section.end - section.start <= limit:
            result.append((SpanPiece(section.start, section.end), section.path))
            continue
        position = section.start
        for start, end, table_id in section_tables:
            result.extend(
                (p, section.path) for p in _body_pieces(document.text, position, start, limit)
            )
            try:
                parts = split_table(
                    document.text, start, end, table_id, size=limit, measure=len, overflow="error"
                )
            except ValueError as exc:
                raise ValueError(
                    "preserved table row or indivisible table exceeds max_parent_chars"
                ) from exc
            result.extend((p, section.path) for p in parts)
            position = end
        result.extend(
            (p, section.path) for p in _body_pieces(document.text, position, section.end, limit)
        )
    return tuple(result)
````

## src/agent_components/chunking/rechunk.py

[打开完整源码](../../src/agent_components/chunking/rechunk.py)

````python
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
````

## src/agent_components/core/markdown_tables.py

[打开完整源码](../../src/agent_components/core/markdown_tables.py)

````python
"""识别规范 Markdown 中的 GFM 表格范围。"""

import re

TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")


def markdown_table_ranges(text: str) -> list[tuple[int, int]]:
    """识别 Markdown 表头、分隔行和连续数据行。

    Args:
        text: 已由 Docling 渲染或调用方提供的 Markdown。

    Returns:
        围栏代码之外的 GFM 表格有序区间。
    """
    lines = text.splitlines(keepends=True)
    positions = [0]
    for line in lines:
        positions.append(positions[-1] + len(line))
    result = []
    index = 0
    fence: tuple[str, int] | None = None
    while index < len(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", lines[index].rstrip("\r\n"))
        if marker:
            symbol, tail = marker.groups()
            if fence is None:
                fence = (symbol[0], len(symbol))
            elif symbol[0] == fence[0] and len(symbol) >= fence[1] and not tail.strip():
                fence = None
            index += 1
            continue
        if (
            fence is None
            and index + 1 < len(lines)
            and "|" in lines[index]
            and TABLE_SEPARATOR.fullmatch(lines[index + 1].strip())
        ):
            end = index + 2
            while end < len(lines) and lines[end].strip() and "|" in lines[end]:
                end += 1
            result.append((positions[index], positions[end]))
            index = end
        else:
            index += 1
    return result
````

## src/agent_components/chunking/tables.py

[打开完整源码](../../src/agent_components/chunking/tables.py)

````python
"""按 GFM 表格行保留结构并重复表头。

正文始终是原文连续切片；后续片的重复表头单独记录，不伪造正文字符偏移。
"""

from collections.abc import Callable
from dataclasses import dataclass

from agent_components.core.contracts import TableSlice
from agent_components.core.markdown_tables import TABLE_SEPARATOR


@dataclass(frozen=True)
class SpanPiece:
    """在分块执行阶段保存区间与额外上下文。

    prefix 的来源记录在 table.header_start/header_end 中。
    """

    start: int
    end: int
    prefix: str = ""
    table: TableSlice | None = None


def split_table(
    text: str,
    start: int,
    end: int,
    table_id: str,
    *,
    size: int,
    measure: Callable[[str], int],
    overflow: str,
) -> list[SpanPiece]:
    """按完整数据行切超长表格，为后续分片重复表头。

    Args:
        text: 完整规范文本。
        start: 表格包含端点。
        end: 表格排除端点。
        table_id: 原生元素身份或 Markdown 表格身份。
        size: 包含重复表头的预算。
        measure: 字符或实际 Token 计数器。
        overflow: error 拒绝无法容纳的一行，allow 显式允许并标记 oversized。

    Returns:
        不切断数据行的片段，带数据行范围和表头来源。

    Raises:
        ValueError: 表头加单行超过预算且 overflow 为 error。
    """
    lines = text[start:end].splitlines(keepends=True)
    header_lines = 2 if len(lines) >= 2 and TABLE_SEPARATOR.fullmatch(lines[1].strip()) else 0
    if not header_lines:
        if measure(text[start:end]) > size and overflow == "error":
            raise ValueError("non-GFM table exceeds size; use table_overflow='allow'")
        return [
            SpanPiece(
                start,
                end,
                table=TableSlice(
                    table_id=table_id,
                    row_start=0,
                    row_end=0,
                    header_start=start,
                    header_end=start,
                    oversized=measure(text[start:end]) > size,
                ),
            )
        ]
    header_end = start + sum(map(len, lines[:header_lines]))
    header = text[start:header_end]
    positions = [header_end]
    for line in lines[header_lines:]:
        positions.append(positions[-1] + len(line))
    pieces: list[SpanPiece] = []
    row = 0
    while row < len(positions) - 1 or (row == 0 and not pieces):
        left = start if not pieces else positions[row]
        # 有后续行时表头已经含原文行终止符，包括 U+2028 等 Unicode 分隔符。
        extra = "" if not pieces else header
        stop = row
        while (
            stop < len(positions) - 1
            and (
                len(extra) + positions[stop + 1] - left
                if measure is len
                else measure(extra + text[left : positions[stop + 1]])
            )
            <= size
        ):
            stop += 1
        if stop == row and row < len(positions) - 1:
            stop += 1
        right = positions[stop]
        oversized = measure(extra + text[left:right]) > size
        if oversized and overflow == "error":
            raise ValueError("table header plus one row exceeds size; use table_overflow='allow'")
        pieces.append(
            SpanPiece(
                left,
                right,
                extra,
                TableSlice(
                    table_id=table_id,
                    row_start=row,
                    row_end=stop,
                    header_start=start,
                    header_end=header_end,
                    oversized=oversized,
                ),
            )
        )
        if stop == len(positions) - 1:
            break
        row = stop
    return pieces
````

## src/agent_components/chunking/sources.py

[打开完整源码](../../src/agent_components/chunking/sources.py)

````python
"""索引规范文本到 Docling 元素、页码和矩形的映射。

按结束位置二分定位候选映射；空白分隔区域仍可定位规范文本，不伪造文件来源。
"""

from bisect import bisect_right
from itertools import islice

from agent_components.core.contracts import Document, SourceRef


class SourceMapper:
    """为一份已校验 Document 创建可复用的位置索引。

    文本范围保持精确；原文件来源保留后端提供的 element/page 精度。
    """

    def __init__(self, document: Document) -> None:
        """预建映射结束位置索引。

        Args:
            document: 结构范围已通过契约校验的文档。
        """
        self._document = document
        self._ends = tuple(item.end for item in document.source_map)

    def resolve(self, start: int, end: int) -> tuple[SourceRef, ...]:
        """读取与给定原文区间相交的来源。

        Args:
            start: 规范文本中的包含端点。
            end: 规范文本中的排除端点。

        Returns:
            精确文本引用及相交元素引用；不插值推算页面局部坐标。

        Raises:
            ValueError: 区间不在文档范围内。
        """
        doc = self._document
        if not 0 <= start < end <= len(doc.text):
            raise ValueError("source lookup must be inside document")
        refs = [
            SourceRef(
                input_id=doc.id,
                document_id=doc.id,
                revision_id=doc.revision_id,
                start=start,
                end=end,
            )
        ]
        index = bisect_right(self._ends, start)
        for span in islice(doc.source_map, index, None):
            if span.start >= end:
                break
            for ref in span.refs:
                # start/end 始终表示规范文本；原生 bbox 保持元素级，不冒充字符级精度。
                refs.append(
                    ref.model_copy(
                        update={"start": max(start, span.start), "end": min(end, span.end)}
                    )
                )
        return tuple(dict.fromkeys(refs))
````

## src/agent_components/parsing/__init__.py

[打开完整源码](../../src/agent_components/parsing/__init__.py)

````python
"""提供文档解析入口和规范文档转换。"""

from agent_components.parsing.docling import DoclingParser

__all__ = ["DoclingParser"]
````

## src/agent_components/parsing/docling.py

[打开完整源码](../../src/agent_components/parsing/docling.py)

````python
"""通过延迟加载的 Docling Adapter 解析文件。

输出以 Markdown 为规范文本，并在部件序列化时建立元素、页码和矩形来源映射。
"""

import hashlib
import os
from collections.abc import Iterable, Iterator
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue

from agent_components.core.contracts import (
    Document,
    DocumentElement,
    ParsedDocument,
    ParseProvenance,
    SourceRef,
    SourceSpan,
)
from agent_components.core.errors import MissingDependencyError
from agent_components.core.markdown_tables import markdown_table_ranges


def _installed_version(distribution: str) -> str | None:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


type _SourceIdentity = tuple[int, int, int, int, int]


def _source_identity(stat: os.stat_result) -> _SourceIdentity:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _check_source(path: Path, identity: _SourceIdentity) -> None:
    try:
        current = _source_identity(path.stat())
    except FileNotFoundError as exc:
        raise ValueError("source changed during document parsing") from exc
    if current != identity:
        raise ValueError("source changed during document parsing")


def _digest_source(path: Path) -> tuple[str, _SourceIdentity]:
    """计算原文件摘要并记录身份，供转换前后检查源文件替换或改写。"""
    with path.open("rb") as reader:
        identity = _source_identity(os.fstat(reader.fileno()))
        digest = hashlib.file_digest(reader, "sha256").hexdigest()
        _check_source(path, identity)
    return digest, identity


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
        model_artifacts_id: str | None = None,
    ) -> None:
        """配置解析资源和 PDF 行为。

        Args:
            artifacts_path: PDF 所需离线模型目录。
            allow_model_downloads: 是否允许 PDF 未指定本地目录时由后端获取模型。
            do_ocr: 是否启用 PDF OCR；默认关闭。
            model_artifacts_id: 可选的模型产物版本或清单摘要，用于 PDF 解析追溯。
        """
        self._artifacts = artifacts_path
        self._allow_downloads = allow_model_downloads
        self._do_ocr = do_ocr
        self._model_artifacts_id = model_artifacts_id

    def parse(self, path: Path, *, document_id: str | None = None) -> ParsedDocument:
        """读取本地文件并构造规范文本。

        Args:
            path: 本地文件路径。
            document_id: 调用方定义的文档身份；省略时使用文件 SHA-256。

        Returns:
            Document、Markdown、原生 JSON；revision_id 为文件内容摘要。

        Raises:
            ValueError: 不支持扩展名、离线 PDF 缺少模型目录或源文件发生变化。
            MissingDependencyError: 非 TXT 解析未安装 parsing extra。
            OSError: 文件读取失败。

        Notes:
            来源按原生元素精度映射；存在目录不代表已验证全部 PDF 权重完整。
        """
        path = Path(path)
        digest, identity = _digest_source(path)
        document_id = document_id or digest
        if path.suffix.lower() == ".txt":
            with path.open(encoding="utf-8-sig", newline="") as reader:
                text = reader.read()
            _check_source(path, identity)
            return ParsedDocument(
                document=Document(
                    id=document_id,
                    text=text,
                    revision_id=digest,
                    metadata={"filename": path.name, "format": "text"},
                ),
                markdown=text,
                native_json={"adapter": "plain_text", "text": text},
                provenance=ParseProvenance(
                    backend="plain_text",
                    backend_version="1",
                    source_sha256=digest,
                    options={"encoding": "utf-8-sig", "preserve_newlines": True},
                ),
            )
        converter = self._converter(path)
        _check_source(path, identity)
        result = converter.convert(path, raises_on_error=True)
        _check_source(path, identity)
        return self.from_native(
            result.document,
            document_id=document_id,
            revision_id=digest,
            filename=path.name,
            provenance=self._provenance(path, digest),
        )

    def _provenance(
        self, path: Path, digest: str, *, page_range: tuple[int, int] | None = None
    ) -> ParseProvenance:
        suffix = path.suffix.lower()
        options: dict[str, JsonValue] = {"format": suffix.removeprefix(".")}
        if suffix == ".pdf":
            options.update(
                accelerator="cpu",
                do_ocr=self._do_ocr,
                allow_model_downloads=self._allow_downloads,
                model_artifacts_id=self._model_artifacts_id,
                model_source="local" if self._artifacts is not None else "backend_default",
                page_range=None if page_range is None else list(page_range),
            )
        return ParseProvenance(
            backend="docling",
            backend_version=_installed_version("docling"),
            source_sha256=digest,
            options=options,
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
            ValueError: 格式或批大小无效、离线资源未配置或源文件发生变化。
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
        digest, identity = _digest_source(path)
        pdf = pypdfium2.PdfDocument(str(path))
        try:
            page_count = len(pdf)
        finally:
            pdf.close()
        _check_source(path, identity)
        source_id = document_id or digest
        for first in range(1, page_count + 1, pages_per_batch):
            last = min(first + pages_per_batch - 1, page_count)
            _check_source(path, identity)
            converted = converter.convert(path, raises_on_error=True, page_range=(first, last))
            _check_source(path, identity)
            parsed = self.from_native(
                converted.document,
                document_id=f"{source_id}#pages={first}-{last}",
                revision_id=digest,
                filename=path.name,
                provenance=self._provenance(path, digest, page_range=(first, last)),
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
        self,
        native: Any,
        *,
        document_id: str,
        revision_id: str,
        filename: str = "",
        provenance: ParseProvenance | None = None,
    ) -> ParsedDocument:
        """把原生 DoclingDocument 转换为可映射的规范 Markdown。

        Args:
            native: 已完成解析的原生 DoclingDocument，可来自离线转换或缓存。
            document_id: 调用方的文档身份。
            revision_id: 原文件或原生 JSON 的内容版本。
            filename: 可选的原文件名。
            provenance: 上游已知解析记录，省略时仅记录原生序列化后端。

        Returns:
            具有结构元素和 SourceMap 的文档；映射精度明确为原生元素级。

        Raises:
            MissingDependencyError: 未安装 parsing extra。
        """
        try:
            from docling_core.transforms.serializer.markdown import MarkdownDocSerializer
        except ImportError as exc:
            raise MissingDependencyError("parsing") from exc
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
            provenance=provenance
            or ParseProvenance(
                backend="docling-native", backend_version=_installed_version("docling-core")
            ),
        )
````

## src/agent_components/chunking/streaming.py

[打开完整源码](../../src/agent_components/chunking/streaming.py)

````python
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
````

## src/agent_components/retrieval/context.py

[打开完整源码](../../src/agent_components/retrieval/context.py)

````python
"""提供父块补充、相邻块扩展和无重复的上下文组装。

仅在调用方提供的可见语料中查找，不替使用方访问数据库、推断权限或执行流程。
"""

from collections import defaultdict
from collections.abc import Sequence

from agent_components.core.contracts import Chunk, Contract, SourceRef


def _merge_source_intervals(refs: Sequence[SourceRef]) -> tuple[SourceRef, ...]:
    """按相同来源锚点合并重叠区间，保留不同页码、元素、精度或矩形。"""
    grouped: dict[SourceRef, list[tuple[int, int]]] = {}
    unlocated = set()
    for ref in refs:
        key = ref.model_copy(update={"start": None, "end": None})
        ranges = grouped.setdefault(key, [])
        if ref.start is not None and ref.end is not None:
            ranges.append((ref.start, ref.end))
        else:
            unlocated.add(key)
    result = []
    for key, ranges in grouped.items():
        if key in unlocated:
            result.append(key)
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        result.extend(key.model_copy(update={"start": start, "end": end}) for start, end in merged)
    return tuple(result)


class ContextWindow(Contract):
    """承载一个文档版本中连续且去除重叠的上下文。

    text 与 [start,end) 对齐；重复表头属于 context_prefix，不改变正文坐标。
    """

    document_id: str
    revision_id: str
    start: int
    end: int
    text: str
    chunk_ids: tuple[str, ...]
    source_refs: tuple[SourceRef, ...]
    context_prefix: str = ""

    @property
    def index_text(self) -> str:
        """读取带独立上下文前缀的正文。

        Returns:
            重复表头与精确正文的组合。
        """
        return self.context_prefix + self.text


class ContextResult(Contract):
    """记录上下文窗口和因预算未包含的块。

    不截断块正文；超过预算的身份在 omitted_ids 中明确返回。
    """

    windows: tuple[ContextWindow, ...]
    selected_ids: tuple[str, ...]
    omitted_ids: tuple[str, ...]


class ContextExpander:
    """在已通过权限与发布版本过滤的块集合中补充上下文。

    相邻查找按文档版本和父块范围隔离，遇到缺失序号立即停止，不跨不可见空隙。
    """

    def expand(
        self,
        hits: Sequence[Chunk],
        corpus: Sequence[Chunk],
        *,
        before: int = 1,
        after: int = 1,
        parent_mode: str = "none",
        stay_in_parent: bool = True,
        max_characters: int = 8000,
        allow_missing_parents: bool = False,
    ) -> ContextResult:
        """补充父块或邻块，按命中顺序应用整体预算。

        Args:
            hits: 检索命中，按相关性排序。
            corpus: 使用方已经过滤权限及版本的块集合，含可选父块。
            before: 每个命中最多向前扩展的叶子数。
            after: 每个命中最多向后扩展的叶子数。
            parent_mode: none 不补父块，append 同时补父块，replace 用父块替换子块。
            stay_in_parent: 默认不跨父块扩展相邻叶子。
            max_characters: 合并后的正文及重复表头总字符上限。
            allow_missing_parents: 父块被权限过滤时保留可见叶子与邻块。

        Returns:
            合并重叠后的窗口、采用的块身份和预算遗漏身份。

        Raises:
            ValueError: 参数无效、命中不在语料中、父关系不合法或重叠正文冲突。
        """
        for value in (before, after):
            if type(value) is not int or value < 0:
                raise ValueError("neighbor counts must be nonnegative integers")
        if type(max_characters) is not int or max_characters < 1:
            raise ValueError("max_characters must be a positive integer")
        if parent_mode not in {"none", "append", "replace"}:
            raise ValueError("invalid parent_mode")
        by_id = {chunk.id: chunk for chunk in corpus}
        if len(by_id) != len(corpus):
            raise ValueError("corpus chunk IDs must be unique")
        leaves = {}
        for chunk in corpus:
            if chunk.kind == "leaf":
                key = (chunk.document_id, chunk.revision_id, chunk.ordinal)
                if key in leaves:
                    raise ValueError("leaf ordinal must be unique within a document version")
                leaves[key] = chunk
        requests = []
        seen = set()
        for hit in hits:
            if by_id.get(hit.id) != hit:
                raise ValueError("hit does not match the visible corpus")
            related = [hit]
            if hit.kind == "leaf":
                for direction, count in ((-1, before), (1, after)):
                    for distance in range(1, count + 1):
                        neighbor = leaves.get(
                            (hit.document_id, hit.revision_id, hit.ordinal + direction * distance)
                        )
                        if neighbor is None or (
                            stay_in_parent and neighbor.parent_id != hit.parent_id
                        ):
                            break
                        related.append(neighbor)
            if parent_mode != "none" and hit.parent_id:
                parent = by_id.get(hit.parent_id)
                if parent is None:
                    if not allow_missing_parents:
                        raise ValueError("parent is missing from visible corpus")
                elif (
                    parent.kind != "parent"
                    or (parent.document_id, parent.revision_id)
                    != (hit.document_id, hit.revision_id)
                    or not parent.start <= hit.start < hit.end <= parent.end
                ):
                    raise ValueError("parent is missing or inconsistent in visible corpus")
                else:
                    related = [parent] if parent_mode == "replace" else related + [parent]
            for chunk in related:
                if chunk.id not in seen:
                    seen.add(chunk.id)
                    requests.append(chunk)
        selected: list[Chunk] = []
        omitted = []
        for chunk in requests:
            windows = self._assemble([*selected, chunk])
            if sum(len(w.index_text) for w in windows) > max_characters:
                omitted.append(chunk.id)
            else:
                selected.append(chunk)
        return ContextResult(
            windows=tuple(
                window.model_copy(
                    update={"source_refs": _merge_source_intervals(window.source_refs)}
                )
                for window in self._assemble(selected)
            ),
            selected_ids=tuple(c.id for c in selected),
            omitted_ids=tuple(omitted),
        )

    def _assemble(self, chunks: Sequence[Chunk]) -> tuple[ContextWindow, ...]:
        """按原文区间合并重叠，验证重复区域的正文一致。

        Args:
            chunks: 已采用的块序列。

        Returns:
            每个文档版本内按位置排序的连续窗口，不填充原文空隙。
        """
        groups: dict[tuple[str, str], list[Chunk]] = defaultdict(list)
        for chunk in chunks:
            groups[(chunk.document_id, chunk.revision_id)].append(chunk)
        result: list[ContextWindow] = []
        for (document_id, revision_id), group in groups.items():
            current: ContextWindow | None = None
            for chunk in sorted(group, key=lambda c: (c.start, -c.end)):
                if current is None or chunk.start > current.end:
                    if current is not None:
                        result.append(current)
                    current = ContextWindow(
                        document_id=document_id,
                        revision_id=revision_id,
                        start=chunk.start,
                        end=chunk.end,
                        text=chunk.text,
                        chunk_ids=(chunk.id,),
                        source_refs=chunk.source_refs,
                        context_prefix=chunk.context_prefix,
                    )
                    continue
                overlap = min(current.end, chunk.end) - chunk.start
                left = chunk.start - current.start
                if current.text[left : left + overlap] != chunk.text[:overlap]:
                    raise ValueError("overlapping chunks have inconsistent canonical text")
                suffix = chunk.text[overlap:] if chunk.end > current.end else ""
                current = current.model_copy(
                    update={
                        "end": max(current.end, chunk.end),
                        "text": current.text + suffix,
                        "chunk_ids": current.chunk_ids + (chunk.id,),
                        "source_refs": tuple(
                            dict.fromkeys((*current.source_refs, *chunk.source_refs))
                        ),
                    }
                )
            if current is not None:
                result.append(current)
        return tuple(result)
````

## src/agent_components/retrieval/selection.py

[打开完整源码](../../src/agent_components/retrieval/selection.py)

````python
"""提供召回后的 MMR、精确及近重复去重组件。

不调用模型或数据库；调用方提供已经过权限与版本过滤的候选及实际向量。
"""

import math
import unicodedata
from collections.abc import Sequence

from pydantic import Field

from agent_components.core.contracts import Chunk, Contract


class RetrievalCandidate(Contract):
    """承载召回候选及可选向量。

    relevance 越大越相关；MMR 使用查询/候选余弦，不直接混合未归一化的 BM25 分数。
    """

    chunk: Chunk
    relevance: float = Field(allow_inf_nan=False)
    vector: tuple[float, ...] | None = None


class SelectionDrop(Contract):
    """记录被去重的候选和保留依据。

    similarity 为规范化文本的 Jaccard；精确重复时为一。
    """

    chunk_id: str
    duplicate_of: str
    similarity: float
    reason: str


class SelectionResult(Contract):
    """返回保留候选与可追溯的去重决定。

    selected 保持输入排序，MMR 的输出则按贪心选择顺序排列。
    """

    selected: tuple[RetrievalCandidate, ...]
    dropped: tuple[SelectionDrop, ...] = ()


def _check_candidates(candidates: Sequence[RetrievalCandidate], maximum: int) -> None:
    if type(maximum) is not int or maximum < 1 or len(candidates) > maximum:
        raise ValueError("candidate count exceeds a positive configured maximum")
    if len({item.chunk.id for item in candidates}) != len(candidates):
        raise ValueError("candidate IDs must be unique")


def _unit(vector: Sequence[float]) -> tuple[float, ...]:
    if len(vector) == 0 or not all(math.isfinite(v) for v in vector):
        raise ValueError("vectors must be nonempty and finite")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("vectors must have a finite nonzero norm")
    return tuple(v / norm for v in vector)


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True))))


def mmr_select(
    candidates: Sequence[RetrievalCandidate],
    query_vector: Sequence[float],
    *,
    limit: int = 10,
    relevance_weight: float = 0.5,
    max_candidates: int = 2000,
    use_candidate_relevance: bool = False,
) -> SelectionResult:
    """执行有界候选集上的最大边际相关性选择。

    Args:
        candidates: 已过滤可见性、ID 唯一且带向量的候选序列。
        query_vector: 与候选使用同一模型和维度的查询向量。
        limit: 最多保留数量。
        relevance_weight: lambda，范围为零到一。
        max_candidates: 拒绝超大候选池，限制 O(n*k) 比较工作量。
        use_candidate_relevance: 使用上游已归一化的融合或重排相关度。

    Returns:
        按 lambda*cos(query,item)-(1-lambda)*max_cos(item,selected) 选择的结果。
        同分时保留输入顺序，不调用额外模型。

    Raises:
        ValueError: 参数、候选身份、向量值或维度无效。
    """
    _check_candidates(candidates, max_candidates)
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    if not math.isfinite(relevance_weight) or not 0 <= relevance_weight <= 1:
        raise ValueError("relevance_weight must be between zero and one")
    # 将查询和候选向量转换为单位向量，之后用点积计算余弦相似度。
    query = _unit(query_vector)
    vectors = []
    for item in candidates:
        if item.vector is None or len(item.vector) != len(query):
            raise ValueError("MMR requires equal-dimensional candidate vectors")
        vectors.append(_unit(item.vector))
    # relevance[i] 用查询与候选的余弦相似度计算，与 LanceDB 返回的距离分数无关。
    relevance = (
        [item.relevance for item in candidates]
        if use_candidate_relevance
        else [_cosine(query, vector) for vector in vectors]
    )
    if use_candidate_relevance and any(not 0 <= score <= 1 for score in relevance):
        raise ValueError("upstream MMR relevance must be normalized to [0, 1]")
    remaining = list(range(len(candidates)))
    selected: list[int] = []
    redundancy = [0.0] * len(candidates)
    while remaining and len(selected) < limit:
        # 这里选出本轮入选的候选。第一轮直接取查询相关性最高的块。
        # 后续得分 = 相关性权重 × 查询相似度 − 多样性权重 × 与已选块的最大相似度。
        # 相似内容会受到惩罚，但这里没有判定重复的阈值，候选仍可能入选。
        best = max(
            remaining,
            key=lambda i: (
                relevance[i]
                if not selected
                else relevance_weight * relevance[i] - (1 - relevance_weight) * redundancy[i]
            ),
        )
        remaining.remove(best)
        selected.append(best)
        # 为下一轮更新每个剩余候选与已选集合的最大相似度。
        # 第一次直接使用本轮相似度，以保留余弦为负的情况。
        for index in remaining:
            value = _cosine(vectors[index], vectors[best])
            redundancy[index] = value if len(selected) == 1 else max(redundancy[index], value)
    return SelectionResult(selected=tuple(candidates[i] for i in selected))


def exact_deduplicate(
    candidates: Sequence[RetrievalCandidate], *, max_candidates: int = 2000
) -> SelectionResult:
    """按排序去除重复文本及同文档版本的完全相同原文区间。"""
    _check_candidates(candidates, max_candidates)
    seen: dict[tuple[str, str, int, int, str], str] = {}
    exact: dict[str, str] = {}
    kept = []
    dropped = []
    for item in candidates:
        chunk = item.chunk
        position = (
            chunk.document_id,
            chunk.revision_id,
            chunk.start,
            chunk.end,
            chunk.context_prefix,
        )
        normalized = _normalized(chunk)
        duplicate = seen.get(position) or exact.get(normalized)
        if duplicate is not None:
            dropped.append(
                SelectionDrop(
                    chunk_id=chunk.id,
                    duplicate_of=duplicate,
                    similarity=1,
                    reason="source_interval" if position in seen else "normalized_exact",
                )
            )
            continue
        seen[position] = exact[normalized] = chunk.id
        kept.append(item)
    return SelectionResult(selected=tuple(kept), dropped=tuple(dropped))


def _normalized(chunk: Chunk) -> str:
    return " ".join(unicodedata.normalize("NFKC", chunk.index_text).casefold().split())


def deduplicate(
    candidates: Sequence[RetrievalCandidate],
    *,
    threshold: float = 0.85,
    shingle_size: int = 3,
    max_candidates: int = 2000,
) -> SelectionResult:
    """按排序保留首次出现的精确或近重复正文。

    Args:
        candidates: 已排序且 ID 唯一的候选，通常由高相关到低相关。
        threshold: 字符 shingles 的 Jaccard 阈值，范围为大于零到一。
        shingle_size: 规范文本的字符 n-gram 长度，默认兼容无空格中文。
        max_candidates: 候选上限，限制两两比较的资源消耗。

    Returns:
        保留候选及每个去重候选的参照身份、相似度和原因。

    Raises:
        ValueError: 阈值、shingle 大小或候选集合无效。
    """
    _check_candidates(candidates, max_candidates)
    if not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1]")
    if type(shingle_size) is not int or shingle_size < 1:
        raise ValueError("shingle_size must be a positive integer")
    kept: list[RetrievalCandidate] = []
    signatures: list[set[str]] = []
    exact: dict[str, str] = {}
    dropped = []
    # 按召回顺序扫描。只有已保留的候选才会作为后续比较的参照。
    for item in candidates:
        # 比较 index_text（上下文前缀加正文），统一全半角、大小写和连续空白。
        normalized = _normalized(item.chunk)
        # 精确去重在这里判定。规范化文本相同时，保留先入选的块，标记当前块为重复项。
        if normalized in exact:
            dropped.append(
                SelectionDrop(
                    chunk_id=item.chunk.id,
                    duplicate_of=exact[normalized],
                    similarity=1,
                    reason="normalized_exact",
                )
            )
            continue
        # 近重复比较用连续字符片段的集合。按默认的三字符长度，
        # “付款期限”得到“付款期”和“款期限”两个片段。
        grams = {
            normalized[i : i + shingle_size]
            for i in range(max(1, len(normalized) - shingle_size + 1))
        }
        duplicate = None
        for index, signature in enumerate(signatures):
            # 交集最多等于较小集合的长度，并集至少等于较大集合的长度。
            # 若按这个上限算也达不到阈值，就跳过这组比较。
            if min(len(grams), len(signature)) / max(len(grams), len(signature)) < threshold:
                continue
            # 这里判定近重复。Jaccard = 片段交集的大小 / 片段并集的大小。
            # 达到阈值就关联第一个满足条件的已保留块。
            similarity = len(grams & signature) / len(grams | signature)
            if similarity >= threshold:
                duplicate = SelectionDrop(
                    chunk_id=item.chunk.id,
                    duplicate_of=kept[index].chunk.id,
                    similarity=similarity,
                    reason="shingle_jaccard",
                )
                break
        # 两种检查都没命中才保留当前块，并让它参与后续比较。
        if duplicate is None:
            exact[normalized] = item.chunk.id
            kept.append(item)
            signatures.append(grams)
        else:
            dropped.append(duplicate)
    return SelectionResult(selected=tuple(kept), dropped=tuple(dropped))
````
