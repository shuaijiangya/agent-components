import asyncio
import json
from contextlib import aclosing

import pytest

pytest.importorskip("agentscope")

from agentscope.credential import OpenAICredential
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import TextBlock, ThinkingBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from pydantic import BaseModel, SecretStr

from agent_components.core.contracts import Message


class NativeModel(ChatModelBase):
    def __init__(self, responses):
        super().__init__(
            model="local-test",
            credential=OpenAICredential(api_key=SecretStr("local-unused")),
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
        )
        self.responses = iter(responses)
        self.calls = []
        self.formatter = OpenAIChatFormatter()

    async def count_tokens(self, *args, **kwargs):
        return 16

    async def _call_api(self, model_name, messages, **kwargs):
        self.calls.append((messages, kwargs))
        item = next(self.responses)
        if asyncio.iscoroutinefunction(item):
            return await item()
        return ChatResponse(
            content=item, is_last=True, usage=ChatUsage(input_tokens=7, output_tokens=2, time=0.1)
        )


class Lookup(BaseModel):
    number: int


def call(arguments='{"number": 42}', call_id="lookup-1"):
    return [ToolCallBlock(id=call_id, name="lookup", input=arguments)]


def request(text="查询订单42"):
    from agent_components.agent import AgentRequest

    return AgentRequest(messages=(Message(role="user", text=text),), run_id="run-1")


async def test_public_stream_calls_native_tool_and_returns_typed_final_result():
    from agent_components.adapters.agentscope import AgentScopeAdapter
    from agent_components.agent import AgentConfig, AgentTool, ToolContext

    async def lookup(params: Lookup, ctx: ToolContext):
        assert ctx.user_id == "operator"
        return {"number": params.number, "status": "ready"}

    model = NativeModel([call(), [ThinkingBlock(thinking="private"), TextBlock(text="订单已就绪")]])
    adapter = AgentScopeAdapter(
        model=model,
        config=AgentConfig(name="orders", system_prompt="用工具查询订单。"),
        tools=[
            AgentTool(
                name="lookup",
                description="查询订单",
                parameters=Lookup,
                handler=lookup,
                read_only=True,
            )
        ],
    )
    events = [
        event async for event in adapter.stream(request(), context=ToolContext(user_id="operator"))
    ]
    kinds = [event.type for event in events]
    assert kinds[0] == "run.started"
    assert kinds[-1] == "run.finished"
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert {event.runtime.id for event in events} == {"run-1"}
    assert {event.request_id for event in events} == {"run-1"}
    assert kinds.index("tool.call.completed") < kinds.index("tool.result.completed")
    assert kinds.count("model.started") == 2
    outcome = events[-1].result
    assert outcome.status == "completed"
    assert outcome.text == "订单已就绪"
    assert outcome.usage.input_tokens == 14
    assert outcome.tool_results[0].call.arguments == {"number": 42}
    assert json.loads(outcome.tool_results[0].text) == {"number": 42, "status": "ready"}
    assert "private" not in "".join(event.model_dump_json() for event in events)


@pytest.mark.parametrize(
    "raw", ['{"number":"42"}', '{"number":1,"number":42}', '{"number":42,"user_id":"other"}']
)
async def test_invalid_model_arguments_never_execute_handler_even_after_native_repair(raw):
    from agent_components.adapters.agentscope import AgentScopeAdapter
    from agent_components.agent import AgentTool

    async def lookup(params, ctx):
        raise AssertionError("invalid arguments reached handler")

    model = NativeModel([call(raw), [TextBlock(text="参数错误")]])
    adapter = AgentScopeAdapter(
        model=model,
        tools=[
            AgentTool(
                name="lookup", description="查询", parameters=Lookup, handler=lookup, read_only=True
            )
        ],
    )
    result = await adapter.run(request())
    assert result.status == "completed"
    assert result.tool_results[0].status == "error"
    assert result.tool_results[0].error.code == "invalid_tool_arguments"


async def test_unapproved_tool_returns_pending_and_never_executes():
    from agent_components.adapters.agentscope import AgentScopeAdapter
    from agent_components.agent import AgentTool

    async def lookup(params, ctx):
        raise AssertionError("unapproved tool ran")

    adapter = AgentScopeAdapter(
        model=NativeModel([call()]),
        tools=[AgentTool(name="lookup", description="受控操作", parameters=Lookup, handler=lookup)],
    )
    result = await adapter.run(request())
    assert result.status == "needs_confirmation"
    assert [call.id for call in result.pending_calls] == ["lookup-1"]
    assert not result.tool_results


def test_non_boolean_read_only_configuration_cannot_authorize_a_tool():
    from agent_components.adapters.agentscope import AgentScopeAdapter
    from agent_components.agent import AgentTool

    async def lookup(params, ctx):
        return None

    with pytest.raises(TypeError, match="read_only must be a boolean"):
        AgentScopeAdapter(
            model=NativeModel([]),
            tools=[
                AgentTool(
                    name="lookup",
                    description="受控操作",
                    parameters=Lookup,
                    handler=lookup,
                    read_only="false",
                )
            ],
        )


@pytest.mark.parametrize("cancel", [True, False])
async def test_timeout_and_cancellation_release_slot(cancel):
    from agent_components.adapters.agentscope import AgentScopeAdapter
    from agent_components.agent import AgentConfig

    started, stopped = asyncio.Event(), asyncio.Event()

    async def block():
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    adapter = AgentScopeAdapter(
        model=NativeModel([block, [TextBlock(text="next run")]]),
        config=AgentConfig(timeout_seconds=0.15, max_concurrency=1),
    )
    task = asyncio.create_task(adapter.run(request()))
    await asyncio.wait_for(started.wait(), timeout=2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).status == "timed_out"
    await asyncio.wait_for(stopped.wait(), timeout=2)
    assert (await adapter.run(request())).text == "next run"


async def test_early_stream_close_releases_run_without_advancing_native_model():
    from agent_components.adapters.agentscope import AgentScopeAdapter
    from agent_components.agent import AgentConfig

    model = NativeModel([[TextBlock(text="fresh")]])
    adapter = AgentScopeAdapter(model=model, config=AgentConfig(max_concurrency=1))
    async with aclosing(adapter.stream(request())) as events:
        assert (await anext(events)).type == "run.started"
        assert (await anext(events)).type == "model.started"
    assert not model.calls
    assert (await adapter.run(request())).text == "fresh"


async def test_native_stream_delivers_text_before_the_model_completes():
    from agent_components.adapters.agentscope import AgentScopeAdapter

    release = asyncio.Event()

    class StreamingModel(NativeModel):
        async def _call_api(self, model_name, messages, **kwargs):
            async def chunks():
                yield ChatResponse(content=[TextBlock(text="订单")], is_last=False)
                await release.wait()
                yield ChatResponse(content=[TextBlock(text="已就绪")], is_last=False)

            return chunks()

    model = StreamingModel([])
    model.stream = True
    async with aclosing(AgentScopeAdapter(model=model).stream(request())) as events:
        async for event in events:
            if event.type == "message.text.delta":
                assert event.delta == "订单"
                break
        release.set()
        tail = [event async for event in events]
    assert [event.delta for event in tail if event.type == "message.text.delta"] == ["已就绪"]
    assert tail[-1].result.text == "订单已就绪"


async def test_backend_and_tool_failures_have_distinct_safe_error_metadata():
    from agent_components.adapters.agentscope import AgentScopeAdapter
    from agent_components.agent import AgentTool

    async def lookup(params, context):
        raise RuntimeError("tool-private-data")

    async def provider_failure():
        raise ConnectionError("model-private-data")

    adapter = AgentScopeAdapter(
        model=NativeModel([call(), provider_failure]),
        tools=[
            AgentTool(
                name="lookup",
                description="查询订单",
                parameters=Lookup,
                handler=lookup,
                read_only=True,
            )
        ],
    )
    result = await adapter.run(request())
    assert result.status == "failed"
    assert result.error.native_type == "ConnectionError"
    assert result.tool_results[0].error.code == "tool_execution_error"
    assert result.tool_results[0].error.native_type == "RuntimeError"
    assert "private-data" not in result.model_dump_json()


async def test_native_iteration_limit_returns_tool_results_and_marks_incomplete_run():
    from agent_components.adapters.agentscope import AgentScopeAdapter
    from agent_components.agent import AgentConfig, AgentTool

    async def lookup(params, context):
        return "ready"

    model = NativeModel([call(), [TextBlock(text="已查询，等待收尾")]])
    adapter = AgentScopeAdapter(
        model=model,
        config=AgentConfig(max_iterations=1),
        tools=[
            AgentTool(
                name="lookup",
                description="查询订单",
                parameters=Lookup,
                handler=lookup,
                read_only=True,
            )
        ],
    )
    result = await adapter.run(request())
    assert result.status == "iteration_limit"
    assert result.tool_results[0].text == "ready"
    assert len(model.calls) == 2
