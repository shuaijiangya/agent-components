"""统一注册本地工具和适配器发现的工具，输出公开 AgentTool 列表。"""

import inspect
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, JsonValue
from referencing.jsonschema import DRAFT202012, UnknownDialect, specification_with

from agent_components.agent.contracts import AgentTool, ToolContext
from agent_components.core.errors import InvalidSchemaError
from agent_components.core.schema import normalize_schema

_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ToolSource(Protocol):
    """将一个工具来源转换成框架无关的工具定义。"""

    async def list_tools(self) -> Sequence[AgentTool[Any]]:
        """读取可注册的工具。来源的连接生命周期由应用管理。"""
        ...


def prepare_tool_schema(tool: AgentTool[Any]) -> dict[str, JsonValue]:
    """检查工具配置并返回独立的对象参数 Schema。"""
    if not isinstance(tool, AgentTool):
        raise TypeError("Only AgentTool instances can be registered")
    if not isinstance(tool.name, str) or not _TOOL_NAME.fullmatch(tool.name):
        raise ValueError(f"Invalid tool name: {tool.name}")
    if not isinstance(tool.description, str) or not tool.description.strip():
        raise ValueError(f"Tool {tool.name} requires a description")
    if not callable(tool.handler):
        raise TypeError(f"Tool {tool.name} requires an asynchronous handler")
    if type(tool.read_only) is not bool:
        raise TypeError(f"Tool {tool.name} read_only must be a boolean")

    parameters = tool.parameters
    is_model = isinstance(parameters, type) and issubclass(parameters, BaseModel)
    if not (isinstance(parameters, dict) or is_model):
        raise TypeError(f"Tool {tool.name} requires a Pydantic parameter model or JSON Schema")
    schema = normalize_schema(parameters)
    if schema.get("type") != "object":
        raise ValueError(f"Tool {tool.name} requires an object parameter schema")
    if is_model:
        # 与本地处理器 model_validate_json(extra="forbid") 的约定一致。
        schema["additionalProperties"] = False
    _check_schema_references(schema)
    return schema


def _check_schema_references(schema: dict[str, JsonValue]) -> None:
    # AgentScope 也会在权限检查前使用这个 Schema。限制其引用域，避免
    # 工具来源提供的远端引用在原生校验时触发隐式网络读取。
    dialect = schema.get("$schema")
    try:
        specification = specification_with(dialect) if isinstance(dialect, str) else DRAFT202012
    except UnknownDialect as exc:
        raise InvalidSchemaError("Unsupported tool schema dialect") from exc
    pending = [specification.create_resource(schema)]
    while pending:
        resource = pending.pop()
        node = resource.contents
        if isinstance(node, dict):
            for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                ref = node.get(keyword)
                if isinstance(ref, str) and not ref.startswith("#"):
                    raise InvalidSchemaError("Tool schema reference must be a local fragment")
        pending.extend(resource.subresources())


def _snapshot(tool: AgentTool[Any]) -> AgentTool[Any]:
    if isinstance(tool.parameters, dict):
        return replace(tool, parameters=deepcopy(tool.parameters))
    return tool


class ToolRegistry:
    """按注册顺序管理工具定义，拒绝重名并对每次批量注册保持原子性。"""

    def __init__(self) -> None:
        """创建独立的工具注册表。"""
        self._tools: dict[str, AgentTool[Any]] = {}

    def register(self, tool: AgentTool[Any]) -> None:
        """注册一个业务工具。"""
        self.register_many((tool,))

    def register_many(self, tools: Iterable[AgentTool[Any]]) -> None:
        """验证整批工具后一起注册，保留声明顺序。"""
        pending: dict[str, AgentTool[Any]] = {}
        for tool in tools:
            prepare_tool_schema(tool)
            if tool.name in self._tools or tool.name in pending:
                raise ValueError(f"Duplicate tool name: {tool.name}")
            pending[tool.name] = _snapshot(tool)
        self._tools.update(pending)

    async def register_source(self, source: ToolSource, *, prefix: str | None = None) -> None:
        """注册一个来源返回的工具，可为实际暴露的工具名添加前缀。

        Args:
            source: 已配置的工具来源，返回 AgentTool 序列。
            prefix: 可选的名称前缀，如 crm 会把 lookup 注册为 crm_lookup。
        """
        if prefix is not None and not _TOOL_NAME.fullmatch(prefix):
            raise ValueError(f"Invalid tool prefix: {prefix}")
        tools = await source.list_tools()
        if prefix is None:
            self.register_many(tools)
        else:
            self.register_many(replace(tool, name=f"{prefix}_{tool.name}") for tool in tools)

    def list_tools(self) -> list[AgentTool[Any]]:
        """返回独立快照，可直接传给 Agent 运行适配器。"""
        return [_snapshot(tool) for tool in self._tools.values()]


DEFAULT_TOOL_REGISTRY = ToolRegistry()


class BaseTool[Params: BaseModel | dict[str, JsonValue]](ABC):
    """将具体子类自动注册为 AgentTool。

    子类定义名称、描述、参数 Schema 并实现异步 execute。工具实例在
    实际调用时通过无参构造创建。应用可在继承时指定 registry 隔离工具。
    """

    name: ClassVar[str]
    description: ClassVar[str]
    parameters: type[Params] | dict[str, JsonValue]
    read_only: ClassVar[bool] = False
    _registry: ClassVar[ToolRegistry] = DEFAULT_TOOL_REGISTRY

    def __init_subclass__(cls, *, registry: ToolRegistry | None = None, **kwargs: Any) -> None:
        """选择注册表并注册具体子类，抽象基类可为后代绑定注册表。"""
        super().__init_subclass__(**kwargs)
        if registry is not None:
            if not isinstance(registry, ToolRegistry):
                raise TypeError("registry must be a ToolRegistry")
            cls._registry = registry
        if inspect.isabstract(cls):
            return
        if not inspect.iscoroutinefunction(cls.execute):
            raise TypeError(f"{cls.__name__}.execute must be async")
        try:
            inspect.signature(cls).bind()
        except TypeError as exc:
            raise TypeError(f"{cls.__name__} must be constructible without arguments") from exc
        parameters = getattr(cls, "parameters", None)
        if parameters is None:
            raise TypeError(f"{cls.__name__} must declare a parameter schema")

        async def invoke(params: Params, context: ToolContext) -> JsonValue:
            return await cls().execute(params, context)

        cls._registry.register(
            AgentTool(
                name=getattr(cls, "name", ""),
                description=getattr(cls, "description", ""),
                parameters=parameters,
                handler=invoke,
                read_only=cls.read_only,
            )
        )

    @abstractmethod
    async def execute(self, params: Params, context: ToolContext) -> JsonValue:
        """执行工具业务逻辑，接收校验后的参数和应用注入的上下文。"""
        ...
