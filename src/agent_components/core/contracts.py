"""定义不依赖具体框架的公共输入输出契约。

对象禁止额外字段且禁止属性重新赋值；嵌套 dict 并非深度不可变。
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class Contract(BaseModel):
    """所有公共数据对象的基础约束。

    禁止未知字段及属性赋值；需要修改时显式构造新对象并重新校验。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


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


class Issue(Contract):
    """携带可机器识别的错误或提示。

    code 用于程序分支，message 用于解释；path 为 JSON Pointer，input_id 指向来源。
    """

    code: str
    message: str
    path: str = ""
    input_id: str | None = None


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


class ParsedDocument(Contract):
    """记录规范文本、Markdown 和后端原生结构。

    source_mapping 为 text_only 时不能把规范文本偏移解释为文件页码或元素坐标。
    """

    document: Document
    markdown: str
    native_json: dict[str, JsonValue]
    source_mapping: Literal["text_only", "mapped"] = "text_only"


class Message(Contract):
    """承载模型消息的角色与正文。

    当前只支持 system、user、assistant 三种文本消息，不表示完整工具事件协议。
    """

    role: Literal["system", "user", "assistant"]
    text: str


class Usage(Contract):
    """记录供应商返回的 Token 用量。

    未提供用量时使用 None，不将未知用量记为零；estimated 表示是否为估算。
    """

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated: bool = False


class ModelRequest(Contract):
    """描述统一模型入口的一次结构化请求。

    schema 为目标 JSON Schema；路由优先使用 model_alias，其次 task，最后默认模型。
    """

    messages: tuple[Message, ...] = Field(min_length=1)
    schema_: dict[str, JsonValue] = Field(alias="schema")
    task: str = "structured_extraction"
    model_alias: str | None = None
    request_id: str | None = None


class ModelOutput(Contract):
    """封装结构化模型输出及用量。

    data 仍须经过抽取组件的 Schema 校验，模型返回 JSON 不等于业务有效。
    """

    data: JsonValue
    usage: Usage = Field(default_factory=Usage)
    model: str | None = None


class Evidence(Contract):
    """记录字段值对应的文本证据及定位精度。

    exact、fuzzy 和 unlocated 必须反映实际匹配结果，不得将近似匹配标为精确。
    """

    field_path: str
    input_id: str
    quote: str
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    alignment: Literal["exact", "fuzzy", "unlocated"]
    source_refs: tuple[SourceRef, ...] = ()


class ExtractionOptions(Contract):
    """控制独立抽取组件的策略与模型路由。

    evidence 需要显式注入证据后端；instructions 为调用方指定的额外抽取要求。
    """

    instructions: str = ""
    strategy: Literal["structured", "evidence"] = "structured"
    model_alias: str | None = None
    request_id: str | None = None


class ExtractionResult(Contract):
    """区分通过校验的数据、未通过候选、证据和问题。

    普通结构化策略失败时 data 为空，原响应保留在 candidates 中供调用方处理。
    """

    status: Literal["success", "partial", "failed"]
    data: JsonValue = None
    candidates: JsonValue = None
    evidence: tuple[Evidence, ...] = ()
    errors: tuple[Issue, ...] = ()
    warnings: tuple[Issue, ...] = ()
    input_ids: tuple[str, ...] = ()
    usage: Usage = Field(default_factory=Usage)


class StateEnvelope(Contract):
    """封装特定后端的版本化状态。

    backend/schema_version 用于兼容检查；payload 不能直接假定可跨框架恢复。
    """

    backend: str
    schema_version: str
    payload: dict[str, JsonValue]


class StoredState(Contract):
    """携带状态内容及并发控制版本。

    revision 从一开始，是存储版本，不是后端状态 Schema 版本。
    """

    namespace: str
    key: str
    revision: int = Field(ge=1)
    state: StateEnvelope


class RuntimeRef(Contract):
    """标识 Adapter 绑定的运行实例。

    id 与 backend 一起用于检查调用是否属于当前运行实例。
    """

    id: str
    backend: str


class PermissionResult(Contract):
    """表达原生权限引擎的判定。

    pending 表示需要后续确认，不表示工具已执行或完整审批流程已实现。
    """

    status: Literal["allow", "deny", "pending"]
    reason: str


class SkillDescriptor(Contract):
    """描述发现的 Skill 及其内容版本。

    version 为包内容摘要；发现结果不代表已挂载或执行。
    """

    id: str
    name: str
    description: str
    version: str
