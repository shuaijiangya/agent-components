"""定义独立于框架的运行、工具和事件契约。"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, Self

from pydantic import BaseModel, Field, JsonValue

from agent_components.core.contracts import Contract, Message, RuntimeRef, Usage


class AgentConfig(Contract):
    """配置 Agent 名称、固定提示词、原生迭代次数及总运行期限。"""

    name: str = Field(default="agent", pattern=r"\S")
    system_prompt: str = ""
    max_iterations: int = Field(default=12, ge=1, strict=True)
    timeout_seconds: float = Field(default=120.0, gt=0, allow_inf_nan=False)
    max_concurrency: int = Field(default=4, ge=1, strict=True)


class AgentRequest(Contract):
    """携带本次运行的消息序列和业务关联标识。"""

    messages: tuple[Message, ...] = Field(min_length=1)
    run_id: str | None = Field(default=None, pattern=r"\S")
    request_id: str | None = Field(default=None, pattern=r"\S")

    @classmethod
    def from_text(
        cls, text: str, *, run_id: str | None = None, request_id: str | None = None
    ) -> Self:
        """用一条用户文本创建请求，可附带业务关联 ID。"""
        if not text.strip():
            raise ValueError("Agent request text is empty")
        return cls(
            messages=(Message(role="user", text=text),), run_id=run_id, request_id=request_id
        )


class ToolContext(Contract):
    """由业务服务注入的可信调用上下文，独立于模型生成的工具参数。"""

    user_id: str | None = None
    tenant_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class AgentTool[Params: BaseModel | dict[str, JsonValue]]:
    """注册一个参数 Schema 和异步处理器。

    Pydantic 参数传入已校验的模型实例。JSON Schema 参数传入已校验的 JSON 对象。
    只读工具可由原生权限引擎直接放行，其他工具默认需要人工确认。
    """

    name: str
    description: str
    parameters: type[Params] | dict[str, JsonValue]
    handler: Callable[[Params, ToolContext], Awaitable[JsonValue]]
    read_only: bool = False


class AgentError(Contract):
    """表达可编程处理的错误码，底层异常仅记录来源类型和 HTTP 状态。"""

    code: str
    message: str
    native_type: str | None = None
    http_status: int | None = None


class MediaContent(Contract):
    """保留原生输出的 MIME 类型和内联或 URL 来源。"""

    media_type: str
    source: Literal["base64", "url"]
    value: str


class AgentMessage(Contract):
    """公开最终回复的标识、文本和媒体内容。"""

    id: str
    role: Literal["assistant"] = "assistant"
    name: str
    text: str = ""
    media: tuple[MediaContent, ...] = ()


class ToolCall(Contract):
    """记录工具调用身份和解析出的参数，原始片段另通过事件传递。"""

    id: str
    name: str
    arguments: dict[str, JsonValue] | None = None


ToolStatus = Literal["success", "error", "denied", "interrupted", "unknown"]


class ToolResult(Contract):
    """关联一条工具调用与原生执行状态及结果。"""

    call: ToolCall
    status: ToolStatus
    text: str = ""
    media: tuple[MediaContent, ...] = ()
    error: AgentError | None = None


RunStatus = Literal[
    "completed",
    "failed",
    "timed_out",
    "interrupted",
    "iteration_limit",
    "needs_confirmation",
    "needs_external_execution",
]


class AgentResult(Contract):
    """记录一次运行的最终状态、回复、已完成工具结果和待处理调用。"""

    runtime: RuntimeRef
    status: RunStatus
    request_id: str | None = None
    reply_id: str | None = None
    message: AgentMessage | None = None
    usage: Usage = Field(default_factory=Usage)
    tool_results: tuple[ToolResult, ...] = ()
    pending_calls: tuple[ToolCall, ...] = ()
    error: AgentError | None = None

    @property
    def text(self) -> str:
        """返回最终回复文本，尚无回复时返回空字符串。"""
        return self.message.text if self.message else ""


EventType = Literal[
    "run.started",
    "model.started",
    "model.completed",
    "message.text.started",
    "message.text.delta",
    "message.text.completed",
    "message.media.started",
    "message.media.delta",
    "message.media.completed",
    "tool.call.started",
    "tool.call.delta",
    "tool.call.completed",
    "tool.result.started",
    "tool.result.delta",
    "tool.result.completed",
    "tool.confirmation_required",
    "tool.external_execution_required",
    "run.finished",
]


class AgentEvent(Contract):
    """统一事件信封；每种事件仅填充与其相关的字段。"""

    type: EventType
    runtime: RuntimeRef
    sequence: int = Field(ge=1)
    request_id: str | None = None
    reply_id: str | None = None
    model: str | None = None
    native_reason: str | None = None
    block_id: str | None = None
    media_type: str | None = None
    delta: str | None = None
    media: MediaContent | None = None
    usage: Usage | None = None
    call: ToolCall | None = None
    tool_result: ToolResult | None = None
    pending_calls: tuple[ToolCall, ...] = ()
    result: AgentResult | None = None


class AgentBackend(Protocol):
    """约定后端提供整次运行和可关闭的流式事件。"""

    async def run(
        self, request: AgentRequest, *, context: ToolContext | None = None
    ) -> AgentResult:
        """执行一次运行并交付完整结果。"""
        ...

    def stream(
        self, request: AgentRequest, *, context: ToolContext | None = None
    ) -> AsyncGenerator[AgentEvent, None]:
        """交付按序事件及最终结果，消费方可主动关闭流。"""
        ...
