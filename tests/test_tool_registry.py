from typing import Any

import pytest
from pydantic import BaseModel


class Lookup(BaseModel):
    number: int


async def lookup(params, context):
    return {"tenant": context.tenant_id, "params": params}


def object_schema(minimum=1):
    return {
        "type": "object",
        "properties": {"number": {"type": "integer", "minimum": minimum}},
        "required": ["number"],
        "additionalProperties": False,
    }


def tool(name="lookup", parameters=Lookup):
    from agent_components.agent import AgentTool

    return AgentTool(
        name=name, description="查询订单", parameters=parameters, handler=lookup, read_only=True
    )


class Source:
    def __init__(self, tools):
        self.tools = tools

    async def list_tools(self):
        return self.tools


async def test_registry_returns_local_and_source_tools_as_an_ordered_list():
    from agent_components.agent import AgentTool, ToolRegistry

    registry = ToolRegistry()
    registry.register(tool())
    await registry.register_source(Source([tool(parameters=object_schema())]), prefix="crm")
    await registry.register_source(Source([tool("lookup")]), prefix="skill")

    tools = registry.list_tools()
    assert isinstance(tools, list)
    assert all(isinstance(item, AgentTool) for item in tools)
    assert [item.name for item in tools] == ["lookup", "crm_lookup", "skill_lookup"]
    assert tools[0].parameters is Lookup
    assert tools[1].parameters == object_schema()


async def test_source_name_conflicts_fail_atomically():
    from agent_components.agent import ToolRegistry

    registry = ToolRegistry()
    registry.register(tool())
    with pytest.raises(ValueError, match="lookup"):
        await registry.register_source(Source([tool("new"), tool()]))
    assert [item.name for item in registry.list_tools()] == ["lookup"]


def test_registry_snapshots_dynamic_schemas_and_returned_lists():
    from agent_components.agent import ToolRegistry

    schema = object_schema()
    registry = ToolRegistry()
    registry.register_many([tool(parameters=schema)])
    schema["properties"]["number"]["minimum"] = 200
    snapshot = registry.list_tools()
    snapshot[0].parameters["properties"]["number"]["minimum"] = 300
    snapshot.clear()
    assert registry.list_tools()[0].parameters == object_schema()


def test_registry_rejects_external_schema_reference():
    from agent_components.agent import ToolRegistry
    from agent_components.core.errors import InvalidSchemaError

    schema = object_schema()
    schema["properties"]["number"] = {"$ref": "https://schemas.example.invalid/id.json"}
    with pytest.raises(InvalidSchemaError, match="reference"):
        ToolRegistry().register(tool(parameters=schema))


@pytest.mark.integration
async def test_dynamic_tool_source_executes_through_native_adapter_with_strict_schema():
    pytest.importorskip("agentscope")
    from test_agent_runtime import NativeModel, TextBlock, call

    from agent_components.adapters.agentscope import AgentScopeAdapter
    from agent_components.agent import AgentRequest, AgentTool, ToolContext, ToolRegistry

    seen = []

    async def handle(params: dict[str, Any], context: ToolContext):
        seen.append((params, context.tenant_id))
        return {"number": params["number"]}

    registry = ToolRegistry()
    schema = object_schema()
    schema["$defs"] = {"Number": {"type": "integer", "minimum": 10}}
    schema["properties"]["number"] = {"$ref": "#/$defs/Number"}
    await registry.register_source(
        Source(
            [
                AgentTool(
                    name="lookup",
                    description="查询订单",
                    parameters=schema,
                    handler=handle,
                    read_only=True,
                )
            ]
        )
    )
    adapter = AgentScopeAdapter(
        model=NativeModel(
            [
                call('{"number":"42"}'),
                call('{"number":42}', call_id="lookup-2"),
                [TextBlock(text="查询完成")],
            ]
        ),
        tools=registry.list_tools(),
    )
    result = await adapter.run(
        AgentRequest.from_text("查询订单42"), context=ToolContext(tenant_id="demo")
    )
    assert result.text == "查询完成"
    assert [row.status for row in result.tool_results] == ["error", "success"]
    assert seen == [({"number": 42}, "demo")]


async def test_concrete_tool_class_autoregisters_in_inherited_registry_with_lazy_instance():
    from agent_components.agent import BaseTool, ToolContext, ToolRegistry

    registry = ToolRegistry()
    created = []

    class OrderTools(BaseTool[Lookup], registry=registry):
        pass

    assert registry.list_tools() == []

    class ReadOrder(OrderTools):
        name = "read_order"
        description = "查询订单"
        parameters = Lookup
        read_only = True

        def __init__(self):
            created.append(object())

        async def execute(self, params: Lookup, context: ToolContext):
            return {"number": params.number, "tenant": context.tenant_id}

    (registered,) = registry.list_tools()
    assert (registered.name, registered.parameters, registered.read_only) == (
        ReadOrder.name,
        Lookup,
        True,
    )
    assert created == []
    assert await registered.handler(Lookup(number=42), ToolContext(tenant_id="demo")) == {
        "number": 42,
        "tenant": "demo",
    }
    assert len(created) == 1


def test_default_registry_and_explicit_registry_use_the_same_duplicate_rules():
    from agent_components.agent import DEFAULT_TOOL_REGISTRY, BaseTool, ToolContext, ToolRegistry

    registry = ToolRegistry()

    class DefaultTool(BaseTool[Lookup]):
        name = "registry_default_fixture"
        description = "默认注册表工具"
        parameters = Lookup

        async def execute(self, params: Lookup, context: ToolContext):
            return None

    class IsolatedTool(DefaultTool, registry=registry):
        pass

    assert [item.name for item in registry.list_tools()] == [IsolatedTool.name]
    assert sum(item.name == DefaultTool.name for item in DEFAULT_TOOL_REGISTRY.list_tools()) == 1

    with pytest.raises(ValueError, match="Duplicate tool name"):

        class DuplicateTool(IsolatedTool):
            pass


def test_class_with_required_constructor_arguments_fails_at_registration():
    from agent_components.agent import BaseTool, ToolContext, ToolRegistry

    registry = ToolRegistry()

    with pytest.raises(TypeError, match="without arguments"):

        class BadTool(BaseTool[Lookup], registry=registry):
            name = "bad"
            description = "配置有误"
            parameters = Lookup

            def __init__(self, dependency):
                self.dependency = dependency

            async def execute(self, params: Lookup, context: ToolContext):
                return None

    assert registry.list_tools() == []
