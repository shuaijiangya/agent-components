"""定义文本模型、流式增量和模型后端能力。"""

from collections.abc import AsyncGenerator
from typing import Protocol

from pydantic import Field

from agent_components.core.contracts import Contract, Message, Usage
from agent_components.core.ports import StructuredModel


class ChatRequest(Contract):
    """传递本次消息和可选的会话标识。

    显式 system_prompt 覆盖客户端默认值，messages 内的系统指令继续参与本次请求。
    携带 session_id 时由客户端读取历史，messages 只提交本轮用户消息及本轮系统指令。
    required_capabilities 列出本次调用所需的模型标签。input_tokens 可传入调用方
    对完整组装输入（含历史与系统指令）的 Token 数，供路由与输出上限一起检查。
    """

    messages: tuple[Message, ...] = Field(min_length=1)
    task: str = "chat"
    model_alias: str | None = None
    request_id: str | None = None
    system_prompt: str | None = None
    session_id: str | None = Field(default=None, pattern=r"\S")
    max_tokens: int | None = Field(default=None, ge=1, strict=True)
    required_capabilities: tuple[str, ...] = ()
    input_tokens: int | None = Field(default=None, ge=0, strict=True)


class ChatOutput(Contract):
    """记录完整助手正文和可用的用量。"""

    text: str = Field(min_length=1)
    usage: Usage = Field(default_factory=Usage)
    model: str | None = None


class TextDelta(Contract):
    """传递新增文本或后端提供的最新用量与模型信息。"""

    text: str = ""
    usage: Usage | None = None
    model: str | None = None


class ChatBackend(Protocol):
    """声明完整聊天响应的供应商接口。"""

    async def chat(self, request: ChatRequest) -> ChatOutput:
        """发送已组装的消息，提供完整成功的文本响应。"""
        ...


class StreamingChatBackend(Protocol):
    """声明供应商的增量文本接口。"""

    def stream_chat(
        self, request: ChatRequest, *, json_mode: bool = False
    ) -> AsyncGenerator[TextDelta, None]:
        """返回可关闭的异步生成器，正常结束仅表示供应商已完整返回。"""
        ...


type ModelBackend = StructuredModel | ChatBackend | StreamingChatBackend
