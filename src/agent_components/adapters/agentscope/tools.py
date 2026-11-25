"""将业务工具封装为 AgentScope FunctionTool，检查模型原始参数。"""

import asyncio
import json
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from copy import deepcopy
from typing import Any, cast

from pydantic import JsonValue, ValidationError

from agent_components.agent.contracts import AgentTool, ToolContext
from agent_components.agent.tools import prepare_tool_schema
from agent_components.core.schema import validate_data

_RESERVED = {"GenerateStructuredOutput", "CompressContext", "reset_tools", "Skill"}


def _unique_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate tool argument keys")
    return result


def _non_json_constant(value: str) -> None:
    raise ValueError("Non-standard JSON constant")


def parse_tool_arguments(raw: str) -> dict[str, JsonValue]:
    """解析最终原始参数，要求 JSON 对象且键唯一。"""
    data = json.loads(raw, object_pairs_hook=_unique_keys, parse_constant=_non_json_constant)
    if not isinstance(data, dict):
        raise ValueError("Tool arguments must be a JSON object")
    # 同时拒绝指数溢出为无穷值的 JSON 数字。
    json.dumps(data, allow_nan=False)
    return cast(dict[str, JsonValue], data)


def prepare_schemas(tools: Sequence[AgentTool[Any]]) -> dict[str, dict[str, JsonValue]]:
    """启动前验证工具名唯一且每个参数模型提供对象 Schema。"""
    schemas: dict[str, dict[str, JsonValue]] = {}
    for tool in tools:
        schema = prepare_tool_schema(tool)
        if tool.name in _RESERVED or tool.name in schemas:
            raise ValueError(f"Reserved or duplicate tool name: {tool.name}")
        schemas[tool.name] = schema
    return schemas


def create_toolkit(
    tools: Sequence[AgentTool[Any]],
    schemas: dict[str, dict[str, JsonValue]],
    context: ToolContext,
) -> Any:
    """创建单次运行独享的原生 Toolkit 和仅含显式注册工具的工具组。"""
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.tool import FunctionTool, ToolChunk, Toolkit, ToolResponse

    def invalid_arguments() -> Any:
        return ToolChunk(
            state=ToolResultState.ERROR,
            content=[TextBlock(text="Tool arguments failed validation.")],
            metadata={"sdk_error": "invalid_tool_arguments"},
        )

    def register(spec: AgentTool[Any]) -> Any:
        async def invoke(**kwargs: JsonValue) -> Any:
            try:
                parameters = spec.parameters
                arguments = (
                    kwargs
                    if isinstance(parameters, dict)
                    else (
                        parameters.model_validate_json(
                            json.dumps(kwargs, ensure_ascii=False, allow_nan=False),
                            strict=True,
                            extra="forbid",
                        )
                    )
                )
            except (ValidationError, ValueError, RecursionError):
                return invalid_arguments()
            try:
                data = await spec.handler(arguments, context.model_copy(deep=True))
                text = (
                    data
                    if isinstance(data, str)
                    else json.dumps(data, ensure_ascii=False, allow_nan=False)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 业务异常正文可能含请求参数或凭据，仅对外暴露错误分类。
                return ToolChunk(
                    state=ToolResultState.ERROR,
                    content=[TextBlock(text="Tool execution failed.")],
                    metadata={
                        "sdk_error": "tool_execution_error",
                        "sdk_error_type": type(exc).__name__,
                    },
                )
            return ToolChunk(state=ToolResultState.SUCCESS, content=[TextBlock(text=text)])

        return FunctionTool(
            invoke,
            name=spec.name,
            description=spec.description,
            input_schema=deepcopy(schemas[spec.name]),
            is_read_only=spec.read_only,
            is_concurrency_safe=False,
        )

    registered = {spec.name: register(spec) for spec in tools}

    class ValidatedToolkit(Toolkit):  # type: ignore[misc]
        async def call_tool(self, tool_call: Any, state: Any) -> AsyncGenerator[Any, None]:
            native = registered.get(tool_call.name)
            if native is None:
                async with aclosing(super().call_tool(tool_call, state)) as chunks:
                    async for chunk in chunks:
                        yield chunk
                return

            # 原生 Agent 已检查工具可用性和权限。直接把原始参数交给已注册的
            # FunctionTool，避免原生 Toolkit 在执行前修复并改变参数。
            try:
                arguments = parse_tool_arguments(tool_call.input)
                invalid = bool(validate_data(arguments, schemas[tool_call.name]))
            except (ValueError, RecursionError):
                chunk = invalid_arguments()
            else:
                chunk = invalid_arguments() if invalid else await native(**arguments)
            yield chunk
            yield ToolResponse(id=tool_call.id).append_chunk(chunk)

    return ValidatedToolkit(tools=list(registered.values()))
