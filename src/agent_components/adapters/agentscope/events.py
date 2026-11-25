"""将 AgentScope 的可见运行事件转换为公共契约。"""

from collections.abc import Mapping
from typing import Any

from pydantic import JsonValue

from agent_components.adapters.agentscope.tools import parse_tool_arguments
from agent_components.agent.contracts import (
    AgentError,
    AgentEvent,
    AgentMessage,
    AgentResult,
    EventType,
    MediaContent,
    RunStatus,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from agent_components.core.contracts import RuntimeRef, Usage
from agent_components.core.schema import validate_data

_TEXT_EVENTS: dict[str, EventType] = {
    "TEXT_BLOCK_START": "message.text.started",
    "TEXT_BLOCK_DELTA": "message.text.delta",
    "TEXT_BLOCK_END": "message.text.completed",
}
_MEDIA_EVENTS: dict[str, EventType] = {
    "DATA_BLOCK_START": "message.media.started",
    "DATA_BLOCK_DELTA": "message.media.delta",
    "DATA_BLOCK_END": "message.media.completed",
}
_TOOL_EVENTS: dict[str, EventType] = {
    "TOOL_CALL_START": "tool.call.started",
    "TOOL_CALL_DELTA": "tool.call.delta",
    "TOOL_CALL_END": "tool.call.completed",
    "TOOL_RESULT_START": "tool.result.started",
    "TOOL_RESULT_TEXT_DELTA": "tool.result.delta",
    "TOOL_RESULT_DATA_DELTA": "tool.result.delta",
    "TOOL_RESULT_END": "tool.result.completed",
}
_RUN_STATUS: dict[str, RunStatus] = {
    "completed": "completed",
    "error": "failed",
    "interrupted": "interrupted",
    "exceed_max_iters": "iteration_limit",
}
_TOOL_STATUS: dict[str, ToolStatus] = {
    "success": "success",
    "error": "error",
    "denied": "denied",
    "interrupted": "interrupted",
}
_TOOL_ERRORS = {
    "invalid_tool_arguments": "Tool arguments failed validation.",
    "tool_execution_error": "The tool handler failed.",
    "tool_error": "The native tool call failed.",
    "tool_denied": "Permission for this tool call was denied.",
    "tool_interrupted": "The tool call was interrupted.",
    "unknown_tool_result": "The native tool returned an unsupported terminal state.",
}


def _arguments(raw: str) -> dict[str, JsonValue] | None:
    try:
        return parse_tool_arguments(raw)
    except (ValueError, RecursionError):
        return None


class RunEvents:
    """按调用 ID 聚合原生流的参数、输出、用量及最终状态。"""

    def __init__(
        self,
        runtime: RuntimeRef,
        schemas: Mapping[str, dict[str, JsonValue]],
        *,
        request_id: str,
    ) -> None:
        """为一次运行单独维护已发送事件和工具状态。"""
        self.runtime = runtime
        self.request_id = request_id
        self._schemas = schemas
        self._sequence = 0
        self._reply_id: str | None = None
        self._model: str | None = None
        self._status: RunStatus | None = None
        self._error: AgentError | None = None
        self._message: AgentMessage | None = None
        self._usage = Usage()
        self._reported_usage: list[Usage] = []
        self._calls: dict[str, ToolCall] = {}
        self._args: dict[str, list[str]] = {}
        self._tool_text: dict[str, list[str]] = {}
        self._tool_media: dict[str, list[MediaContent]] = {}
        self._tool_results: list[ToolResult] = []
        self._pending: dict[str, ToolCall] = {}

    def event(self, kind: EventType, **fields: Any) -> AgentEvent:
        """分配序号并创建公开事件。"""
        self._sequence += 1
        return AgentEvent(
            type=kind,
            runtime=self.runtime,
            sequence=self._sequence,
            request_id=self.request_id,
            reply_id=self._reply_id,
            **fields,
        )

    def message(self, native: Any) -> None:
        """保留最后一条回复的公开文本、媒体及供应商用量。"""
        self._reply_id = self._reply_id or native.id
        self._message = AgentMessage(
            id=native.id,
            name=native.name,
            text="".join(block.text for block in native.content if block.type == "text"),
            media=tuple(
                MediaContent(
                    media_type=block.source.media_type,
                    source=block.source.type,
                    value=block.source.data
                    if block.source.type == "base64"
                    else str(block.source.url),
                )
                for block in native.content
                if block.type == "data"
            ),
        )
        if native.usage is not None:
            self._usage = Usage(
                input_tokens=native.usage.input_tokens, output_tokens=native.usage.output_tokens
            )

    def convert(self, native: Any) -> AgentEvent | None:
        """转换原生事件。内部提示、推理块和自定义扩展不进入公共响应。"""
        self._reply_id = getattr(native, "reply_id", None) or self._reply_id
        kind = native.type
        if kind == "REPLY_END":
            reason = str(native.finished_reason)
            self._status = _RUN_STATUS.get(reason, "failed")
            if self._status == "failed":
                self._error = AgentError(
                    code="agent_backend_error",
                    message="The native Agent could not complete.",
                    native_type=str(native.error.type) if native.error else reason,
                )
        elif kind == "MODEL_CALL_START":
            self._model = native.model_name
            return self.event("model.started", model=self._model)
        elif kind == "MODEL_CALL_END":
            usage = Usage()
            # 本版本原生事件在供应商未报告用量时把两个字段填成 0。
            if native.input_tokens or native.output_tokens:
                usage = Usage(input_tokens=native.input_tokens, output_tokens=native.output_tokens)
            self._reported_usage.append(usage)
            return self.event(
                "model.completed",
                model=self._model,
                usage=usage,
                native_reason=str(native.finished_reason),
            )
        elif kind in _TEXT_EVENTS:
            delta = getattr(native, "delta", None) or getattr(native, "text", None)
            return self.event(_TEXT_EVENTS[kind], block_id=native.block_id, delta=delta)
        elif kind in _MEDIA_EVENTS:
            data = getattr(native, "data", None)
            media_type = getattr(native, "media_type", None)
            media = (
                None
                if data is None or media_type is None
                else MediaContent(
                    media_type=media_type,
                    source="base64",
                    value=data,
                )
            )
            return self.event(
                _MEDIA_EVENTS[kind],
                block_id=native.block_id,
                media_type=media_type,
                media=media,
            )
        elif kind in _TOOL_EVENTS:
            return self._tool_event(native, _TOOL_EVENTS[kind])
        elif kind in ("REQUIRE_USER_CONFIRM", "REQUIRE_EXTERNAL_EXECUTION"):
            calls = tuple(
                ToolCall(id=call.id, name=call.name, arguments=_arguments(call.input))
                for call in native.tool_calls
            )
            self._pending.update((call.id, call) for call in calls)
            if kind == "REQUIRE_USER_CONFIRM":
                self._status = "needs_confirmation"
                return self.event("tool.confirmation_required", pending_calls=calls)
            self._status = "needs_external_execution"
            return self.event("tool.external_execution_required", pending_calls=calls)
        return None

    def _tool_event(self, native: Any, event_type: EventType) -> AgentEvent:
        call_id: str = native.tool_call_id
        call = self._calls.get(call_id)
        if call is None:
            call = ToolCall(id=call_id, name=getattr(native, "tool_call_name", ""))
            self._calls[call_id] = call
        if event_type == "tool.call.started":
            self._args[call_id] = []
        elif event_type == "tool.call.delta":
            self._args.setdefault(call_id, []).append(native.delta)
            return self.event(event_type, call=call, delta=native.delta)
        elif event_type == "tool.call.completed":
            raw = "".join(self._args.pop(call_id, ()))
            call = ToolCall(id=call.id, name=call.name, arguments=_arguments(raw))
            self._calls[call_id] = call
        elif event_type == "tool.result.delta":
            text = getattr(native, "delta", None)
            if text is not None:
                self._tool_text.setdefault(call_id, []).append(text)
                return self.event(event_type, call=call, delta=text)
            data = getattr(native, "data", None)
            media = MediaContent(
                media_type=native.media_type,
                source="base64" if data is not None else "url",
                value=data if data is not None else native.url,
            )
            self._tool_media.setdefault(call_id, []).append(media)
            return self.event(event_type, call=call, block_id=native.block_id, media=media)
        elif event_type == "tool.result.completed":
            result = self._finish_tool(call, native)
            self._tool_results.append(result)
            return self.event(event_type, call=call, tool_result=result)
        return self.event(event_type, call=call)

    def _finish_tool(self, call: ToolCall, native: Any) -> ToolResult:
        status = _TOOL_STATUS.get(str(native.state), "unknown")
        error = None
        if status != "success":
            code = {
                "denied": "tool_denied",
                "interrupted": "tool_interrupted",
                "unknown": "unknown_tool_result",
            }.get(status, "tool_error")
            sdk_code = native.metadata.get("sdk_error")
            if sdk_code in _TOOL_ERRORS:
                code = sdk_code
            elif status == "error" and (schema := self._schemas.get(call.name)) is not None:
                if call.arguments is None or validate_data(call.arguments, schema):
                    code = "invalid_tool_arguments"
            error = AgentError(
                code=code,
                message=_TOOL_ERRORS[code],
                native_type=native.metadata.get("sdk_error_type"),
            )
        return ToolResult(
            call=call,
            status=status,
            text="".join(self._tool_text.pop(call.id, ())),
            media=tuple(self._tool_media.pop(call.id, ())),
            error=error,
        )

    def finish(
        self, *, status: RunStatus | None = None, error: AgentError | None = None
    ) -> AgentEvent:
        """发出单条终态事件，保留超时或失败之前已有的工具结果。"""
        final_status = status or self._status or "failed"
        problem = error or self._error
        if final_status == "completed" and self._message is None:
            final_status = "failed"
        if final_status == "failed" and problem is None:
            problem = AgentError(
                code="incomplete_reply", message="The native reply was incomplete."
            )
        usage = self._usage
        if usage.input_tokens is None and self._reported_usage:
            rows = self._reported_usage
            inputs = [row.input_tokens for row in rows if row.input_tokens is not None]
            outputs = [row.output_tokens for row in rows if row.output_tokens is not None]
            usage = Usage(
                input_tokens=sum(inputs) if len(inputs) == len(rows) else None,
                output_tokens=sum(outputs) if len(outputs) == len(rows) else None,
            )
        return self.event(
            "run.finished",
            result=AgentResult(
                runtime=self.runtime,
                status=final_status,
                request_id=self.request_id,
                reply_id=self._reply_id,
                message=self._message if final_status in ("completed", "iteration_limit") else None,
                usage=usage,
                tool_results=tuple(self._tool_results),
                pending_calls=tuple(self._pending.values()),
                error=problem,
            ),
        )
