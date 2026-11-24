"""统一模型选路、消息组装、会话历史和逻辑调用。"""

import json
import math
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import aclosing
from itertools import takewhile
from typing import cast, overload

from pydantic import SecretStr

from agent_components.core.contracts import Message, ModelOutput, ModelRequest, Usage
from agent_components.core.errors import UnsupportedCapabilityError
from agent_components.core.ports import StructuredModel
from agent_components.core.schema import normalize_schema, validate_data
from agent_components.execution import ExecutionController
from agent_components.models.call_logging import ModelCall
from agent_components.models.contracts import (
    ChatBackend,
    ChatOutput,
    ChatRequest,
    ModelBackend,
    StreamingChatBackend,
    TextDelta,
)
from agent_components.models.dispatch import dispatch
from agent_components.models.memory import ConversationMemory, MemorySnapshot
from agent_components.models.routing import ModelProfile, _ModelRouter


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Structured streams require finite JSON numbers")
    return number


class ModelClient:
    """通过别名和任务规则统一调用聊天与结构化模型。

    系统指令与会话历史在调用供应商前组装。聊天通过显式 session_id
    选择历史；成功完成的用户和助手消息作为一轮提交。
    """

    def __init__(
        self,
        models: Mapping[str, ModelBackend],
        *,
        default_alias: str,
        task_routes: Mapping[str, str] | None = None,
        profiles: Mapping[str, ModelProfile] | None = None,
        fallback_routes: Mapping[str, Sequence[str]] | None = None,
        log_secrets: Sequence[str | SecretStr] = (),
        execution: ExecutionController | None = None,
        system_prompt: str | None = None,
        memory: ConversationMemory | None = None,
        max_history_turns: int = 12,
    ) -> None:
        """装配路由、执行策略和可选会话后端。

        Args:
            models: 模型别名与相应能力后端。
            default_alias: 无显式别名和任务规则时使用的模型。
            task_routes: 任务名称到模型别名的静态映射。
            profiles: 按别名声明能力和上下文窗口。
            fallback_routes: 按优先级排列的备用别名。
            log_secrets: 正文日志脱敏时要屏蔽的已知凭据。
            execution: 并发、超时和重试控制器。
            system_prompt: 默认系统指令，可由单次请求覆盖。
            memory: 支持版本条件写入的会话历史后端。
            max_history_turns: 加载和保存的历史轮次上限。
        """
        if type(max_history_turns) is not int or max_history_turns < 1:
            raise ValueError("max_history_turns must be a positive integer")
        self._router = _ModelRouter(
            models, default_alias, task_routes, profiles=profiles, fallback_routes=fallback_routes
        )
        known_secrets = list(log_secrets)
        for backend in models.values():
            secrets = getattr(backend, "log_secrets", ())
            if isinstance(secrets, tuple):
                known_secrets.extend(
                    secret for secret in secrets if isinstance(secret, (str, SecretStr))
                )
        self._log_secrets = tuple(known_secrets)
        self._execution = execution or ExecutionController()
        self._system_prompt = system_prompt
        self._memory = memory
        self._history_size = max_history_turns * 2

    def _messages(
        self,
        request: ChatRequest | ModelRequest,
        history: tuple[Message, ...] = (),
        schema_instruction: str = "",
    ) -> tuple[Message, ...]:
        """在前置系统指令之后插入历史，保留本次消息顺序。"""
        leading = tuple(takewhile(lambda item: item.role == "system", request.messages))
        remaining = request.messages[len(leading) :]
        prompt = self._system_prompt if request.system_prompt is None else request.system_prompt
        if prompt or schema_instruction:
            # 合并前置系统指令，兼容要求单条开头 system 消息的模型。
            parts = [prompt or "", *(item.text for item in leading), schema_instruction]
            leading = (Message(role="system", text="\n\n".join(part for part in parts if part)),)
        return (*leading, *history, *remaining)

    async def _chat_request(
        self, request: ChatRequest
    ) -> tuple[ChatRequest, MemorySnapshot | None]:
        snapshot = None
        if request.session_id is not None:
            if self._memory is None:
                raise UnsupportedCapabilityError("Conversation memory is not configured")
            current = tuple(item for item in request.messages if item.role != "system")
            if len(current) != 1 or current[0].role != "user" or request.messages[-1] != current[0]:
                raise ValueError("A session request must contain exactly one user message")
            snapshot = await self._memory.load(request.session_id)
        history = snapshot.messages[-self._history_size :] if snapshot is not None else ()
        return (
            request.model_copy(
                update={
                    "system_prompt": None,
                    "session_id": None,
                    "messages": self._messages(request, history),
                }
            ),
            snapshot,
        )

    async def _save_turn(
        self, request: ChatRequest, snapshot: MemorySnapshot | None, output: ChatOutput
    ) -> None:
        if snapshot is None:
            return
        assert self._memory is not None and request.session_id is not None
        messages = (
            *snapshot.messages,
            request.messages[-1],
            Message(role="assistant", text=output.text),
        )
        # 只提交一个完整的新轮次。并发更新时由存储比较版本，拒绝覆盖另一轮。
        await self._memory.save(
            request.session_id, messages[-self._history_size :], expected_revision=snapshot.revision
        )

    def _call(self, request: ChatRequest | ModelRequest, operation: str) -> ModelCall:
        return ModelCall(
            request.task,
            request_id=request.request_id,
            alias=request.model_alias,
            operation=operation,
            secrets=self._log_secrets,
            body=[msg.model_dump(mode="json") for msg in request.messages],
        )

    async def generate(self, request: ModelRequest) -> ModelOutput:
        """按规则请求结构化输出，所有候选模型共享尝试次数。"""
        with self._call(request, "generate") as call:
            prepared = request.model_copy(
                update={"messages": self._messages(request), "system_prompt": None}
            )

            async def invoke(backend: ModelBackend) -> AsyncGenerator[ModelOutput, None]:
                yield await cast(StructuredModel, backend).generate(prepared)

            async with aclosing(
                dispatch(self._router, self._execution, prepared, "generate", call, invoke)
            ) as stream:
                async for output in stream:
                    result = output
            call.complete(model=result.model, usage=result.usage, body=result.data)
            return result

    async def chat(self, request: ChatRequest) -> ChatOutput:
        """按包含系统指令和会话历史的输入选模，成功后提交一轮历史。"""
        with self._call(request, "chat") as call:
            prepared, snapshot = await self._chat_request(request)

            async def invoke(backend: ModelBackend) -> AsyncGenerator[ChatOutput, None]:
                yield await cast(ChatBackend, backend).chat(prepared)

            async with aclosing(
                dispatch(self._router, self._execution, prepared, "chat", call, invoke)
            ) as stream:
                async for output in stream:
                    result = output
            await self._save_turn(request, snapshot, result)
            call.complete(model=result.model, usage=result.usage, body=result.text)
            return result

    def stream_chat(self, request: ChatRequest) -> AsyncGenerator[TextDelta | ChatOutput, None]:
        """返回文本增量和最后一个完整结果，提前退出时用 aclosing 释放请求。"""
        return self._stream(request)

    def stream_generate(
        self, request: ModelRequest
    ) -> AsyncGenerator[TextDelta | ModelOutput, None]:
        """返回 JSON 预览，流正常完成且通过 Schema 校验后发送最终结果。"""
        return self._stream(request)

    @overload
    def _stream(self, request: ChatRequest) -> AsyncGenerator[TextDelta | ChatOutput, None]: ...

    @overload
    def _stream(self, request: ModelRequest) -> AsyncGenerator[TextDelta | ModelOutput, None]: ...

    async def _stream(
        self, request: ModelRequest | ChatRequest
    ) -> AsyncGenerator[TextDelta | ChatOutput | ModelOutput, None]:
        operation = "stream_generate" if isinstance(request, ModelRequest) else "stream_chat"
        with self._call(request, operation) as call:
            schema = None
            snapshot = None
            if isinstance(request, ModelRequest):
                schema = normalize_schema(request.schema_)
                if schema.get("type") != "object":
                    raise UnsupportedCapabilityError("Streamed JSON requires an object root schema")
                instruction = "Return only a JSON object matching this schema: " + json.dumps(
                    schema, ensure_ascii=False
                )
                prepared = ChatRequest(
                    messages=self._messages(request, schema_instruction=instruction),
                    task=request.task,
                    model_alias=request.model_alias,
                    request_id=request.request_id,
                    required_capabilities=request.required_capabilities,
                    input_tokens=request.input_tokens,
                    max_tokens=request.max_tokens,
                )
            else:
                prepared, snapshot = await self._chat_request(request)

            async def invoke(
                backend: ModelBackend,
            ) -> AsyncGenerator[TextDelta | ModelOutput | ChatOutput, None]:
                parts: list[str] = []
                usage = Usage()
                model = None
                provider = cast(StreamingChatBackend, backend)
                async with aclosing(
                    provider.stream_chat(prepared, json_mode=schema is not None)
                ) as chunks:
                    async for delta in chunks:
                        if delta.usage is not None:
                            usage = delta.usage
                        if delta.model is not None:
                            model = delta.model
                        if delta.text:
                            parts.append(delta.text)
                            yield TextDelta(text=delta.text)
                text = "".join(parts)
                if schema is not None:
                    data = json.loads(
                        text, parse_float=_finite_json_float, parse_constant=_finite_json_float
                    )
                    if validate_data(data, schema):
                        raise ValueError("Streamed output does not match the target schema")
                    yield ModelOutput(data=data, usage=usage, model=model)
                else:
                    yield ChatOutput(text=text, usage=usage, model=model)

            async with aclosing(
                dispatch(self._router, self._execution, prepared, operation, call, invoke)
            ) as stream:
                async for item in stream:
                    if isinstance(item, TextDelta):
                        yield item
                    else:
                        output = item
            if isinstance(output, ChatOutput):
                assert isinstance(request, ChatRequest)
                await self._save_turn(request, snapshot, output)
            call.complete(
                model=output.model,
                usage=output.usage,
                body=output.text if isinstance(output, ChatOutput) else output.data,
            )
        yield output
