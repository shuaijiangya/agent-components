"""适配 AgentScope 2.0.11 的模型级结构化输出和文本聊天。

不需要创建 Agent 实例，仅转换消息、输出和用量。
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, cast

from pydantic import JsonValue

from agent_components.core.contracts import Message, ModelOutput, ModelRequest, Usage
from agent_components.core.errors import MissingDependencyError, UnsupportedCapabilityError
from agent_components.models.contracts import ChatOutput, ChatRequest, TextDelta


def _native_messages(messages: tuple[Message, ...]) -> list[Any]:
    try:
        from agentscope.message import AssistantMsg, SystemMsg, UserMsg
    except ImportError as exc:
        raise MissingDependencyError("agent") from exc

    constructors: dict[str, Any] = {
        "system": SystemMsg,
        "user": UserMsg,
        "assistant": AssistantMsg,
    }
    return [constructors[item.role](name=item.role, content=item.text) for item in messages]


def _native_usage(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    return Usage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)


def _openai_usage(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    return Usage(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
    )


def _choice_zero(response: Any) -> Any:
    choices = getattr(response, "choices", None) or ()
    return next((choice for choice in choices if choice.index == 0), None)


class AgentScopeStructuredModel:
    """封装原生支持结构化输出的模型对象。

    当前只支持 object 根 Schema；供应商异常保持原样传播。
    """

    def __init__(self, model: Any) -> None:
        """校验并绑定原生模型。

        Args:
            model: 提供 generate_structured_output 的原生模型。

        Raises:
            TypeError: 对象缺少可调用的结构化输出方法。
        """
        if not callable(getattr(model, "generate_structured_output", None)):
            raise TypeError("model must support generate_structured_output")
        self._model = model

    async def generate(self, request: ModelRequest) -> ModelOutput:
        """转换统一消息并请求原生结构化输出。

        Args:
            request: 消息及 object 根 JSON Schema。

        Returns:
            公共模型响应和供应商实际用量。

        Raises:
            UnsupportedCapabilityError: 根 Schema 不是 object。
            MissingDependencyError: 未安装 agent extra。
        """
        if request.schema_.get("type") != "object":
            raise UnsupportedCapabilityError(
                "AgentScope adapter currently requires object root schema"
            )
        response = await self._model.generate_structured_output(
            messages=_native_messages(request.messages),
            structured_model=request.schema_,
            **self._native_token_options(request.max_tokens),
        )
        return ModelOutput(
            data=cast(JsonValue, response.content),
            usage=_native_usage(response.usage),
            model=getattr(self._model, "model", None),
        )

    async def chat(self, request: ChatRequest) -> ChatOutput:
        """返回供应商正常结束的完整文本响应。"""
        create = self._openai_completion_create()
        if callable(create):
            # AgentScope 的 OpenAI 转换会丢弃 finish_reason 和实际模型名。
            response = await create(stream=False, **self._openai_options(request))
            choice = _choice_zero(response)
            if choice is None or choice.finish_reason != "stop":
                raise RuntimeError("Provider chat did not finish with stop")
            return ChatOutput(
                text=choice.message.content or "",
                usage=_openai_usage(getattr(response, "usage", None)),
                model=getattr(response, "model", None) or self._model.model,
            )

        if not callable(self._model) or getattr(self._model, "stream", False):
            raise UnsupportedCapabilityError("AgentScope backend does not support nonstream chat")
        try:
            from agentscope.message import TextBlock
            from agentscope.model import ChatResponse, FinishedReason
        except ImportError as exc:
            raise MissingDependencyError("agent") from exc

        response = await self._model(
            messages=_native_messages(request.messages),
            **self._native_token_options(request.max_tokens),
        )
        if isinstance(response, ChatResponse):
            if response.finished_reason == FinishedReason.INTERRUPTED:
                raise asyncio.CancelledError
            if response.is_last and response.finished_reason == FinishedReason.COMPLETED:
                return ChatOutput(
                    text="".join(
                        block.text for block in response.content if isinstance(block, TextBlock)
                    ),
                    usage=_native_usage(response.usage),
                    model=getattr(self._model, "model", None),
                )
        raise RuntimeError("AgentScope chat did not finish normally")

    async def stream_chat(
        self, request: ChatRequest, *, json_mode: bool = False
    ) -> AsyncGenerator[TextDelta, None]:
        """透传 OpenAI 兼容端点的可见文本增量及可选用量。"""
        create = self._openai_completion_create()
        if not callable(create):
            raise UnsupportedCapabilityError(
                "AgentScope backend does not support OpenAI chat streams"
            )

        options = self._openai_options(request)
        if json_mode:
            options["response_format"] = {"type": "json_object"}

        stopped = False
        model: str | None = None
        reported_model: str | None = None
        async with await create(
            stream=True, stream_options={"include_usage": True}, **options
        ) as stream:
            async for chunk in stream:
                model = getattr(chunk, "model", None) or model
                usage = getattr(chunk, "usage", None)
                choice = _choice_zero(chunk)
                text = ""
                if choice is not None:
                    if choice.finish_reason is not None:
                        if choice.finish_reason != "stop":
                            raise RuntimeError("Provider chat stream did not finish with stop")
                        stopped = True
                    text = getattr(choice.delta, "content", None) or ""
                if text or usage is not None:
                    reported_model = model
                    yield TextDelta(
                        text=text,
                        usage=_openai_usage(usage) if usage is not None else None,
                        model=model,
                    )

        if not stopped:
            raise RuntimeError("Provider chat stream ended without a stop finish reason")
        if model != reported_model:
            yield TextDelta(model=model)

    def _openai_completion_create(self) -> Any:
        # OpenAI Responses 模型也使用提供 chat.completions 的 OpenAI 客户端。
        if getattr(self._model, "type", None) != "openai_chat":
            return None
        client = getattr(self._model, "client", None)
        chat = getattr(client, "chat", None)
        completions = getattr(chat, "completions", None)
        return getattr(completions, "create", None)

    def _native_token_options(self, max_tokens: int | None) -> dict[str, int]:
        if max_tokens is None:
            return {}
        if getattr(self._model, "type", None) == "openai_response":
            return {"max_output_tokens": max_tokens}
        return {"max_tokens": max_tokens}

    def _openai_options(self, request: ChatRequest) -> dict[str, Any]:
        options: dict[str, Any] = {
            "model": self._model.model,
            "messages": [{"role": item.role, "content": item.text} for item in request.messages],
        }
        if request.max_tokens is not None:
            options["max_tokens"] = request.max_tokens
        return options
