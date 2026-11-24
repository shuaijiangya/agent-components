import asyncio
import json
import logging
from contextlib import aclosing

import pytest

from agent_components.core.errors import RevisionConflictError, UnsupportedCapabilityError
from agent_components.execution import ExecutionController, ExecutionPolicy
from agent_components.models import Message, ModelClient, ModelOutput, ModelRequest, Usage


class Backend:
    def __init__(self):
        self.requests = []
        self.closed = False
        self.stream_body = "完整回复"
        self.stream_error = None

    async def generate(self, request):
        self.requests.append(request)
        return ModelOutput(data={"amount": 32000})

    async def chat(self, request):
        from agent_components.models import ChatOutput

        self.requests.append(request)
        return ChatOutput(text="非流式回复", model="actual-model")

    async def stream_chat(self, request, *, json_mode=False):
        from agent_components.models import TextDelta

        self.requests.append(request)
        try:
            yield TextDelta(text=self.stream_body)
            if self.stream_error is not None:
                raise self.stream_error
            yield TextDelta(usage=Usage(input_tokens=5, output_tokens=2), model="actual-model")
        finally:
            self.closed = True


class Memory:
    def __init__(self):
        self.sessions = {}

    async def load(self, session_id):
        from agent_components.models import MemorySnapshot

        return self.sessions.get(session_id, MemorySnapshot())

    async def save(self, session_id, messages, *, expected_revision):
        from agent_components.models import MemorySnapshot

        stored = await self.load(session_id)
        if stored.revision != expected_revision:
            raise RevisionConflictError("memory changed")
        self.sessions[session_id] = MemorySnapshot(
            revision=expected_revision + 1, messages=messages
        )


def user(text):
    return Message(role="user", text=text)


def system(text):
    return Message(role="system", text=text)


def assistant(text):
    return Message(role="assistant", text=text)


async def test_system_prompt_override_preserves_existing_extraction_instruction():
    backend = Backend()
    client = ModelClient({"one": backend}, default_alias="one", system_prompt="应用默认")
    request = ModelRequest(
        messages=(system("抽取约束"), user("合同原文")),
        schema={"type": "object"},
        system_prompt="单次系统指令",
    )
    await client.generate(request)
    assert backend.requests[-1].messages == (system("单次系统指令\n\n抽取约束"), user("合同原文"))
    await client.generate(request.model_copy(update={"system_prompt": None}))
    assert backend.requests[-1].messages == (system("应用默认\n\n抽取约束"), user("合同原文"))
    assert request.messages[0].text == "抽取约束"


async def test_stream_chat_recalls_only_session_local_history_and_commits_final_reply(caplog):
    from agent_components.models import ChatOutput, ChatRequest, TextDelta

    backend = Backend()
    memory = Memory()
    client = ModelClient(
        {"one": backend}, default_alias="one", memory=memory, system_prompt="应用默认"
    )
    request = ChatRequest(
        messages=(user("首轮私密正文"),), session_id="session-a", system_prompt="首轮规则"
    )
    await client.chat(request)
    with caplog.at_level(logging.INFO, logger="agent_components.model_calls"):
        stream = client.stream_chat(
            ChatRequest(messages=(user("继续"),), session_id="session-a", request_id="stream-1")
        )
        async with aclosing(stream):
            assert await anext(stream) == TextDelta(text="完整回复")
            assert memory.sessions["session-a"].revision == 1
            final = await anext(stream)
            assert final == ChatOutput(
                text="完整回复", usage=Usage(input_tokens=5, output_tokens=2), model="actual-model"
            )
            assert (await memory.load("session-a")).messages == (
                user("首轮私密正文"),
                assistant("非流式回复"),
                user("继续"),
                assistant("完整回复"),
            )
    assert backend.requests[-1].messages == (
        system("应用默认"),
        user("首轮私密正文"),
        assistant("非流式回复"),
        user("继续"),
    )
    await client.chat(ChatRequest(messages=(user("新会话"),), session_id="session-b"))
    assert backend.requests[-1].messages == (system("应用默认"), user("新会话"))
    assert "首轮私密正文" not in caplog.text and "session-a" not in caplog.text
    assert [json.loads(record.message)["event"] for record in caplog.records] == [
        "model.started",
        "model.route",
        "model.attempt.started",
        "model.attempt.completed",
        "model.completed",
    ]


@pytest.mark.parametrize("failure", ["close", "provider", "cancel"])
async def test_partial_or_interrupted_stream_keeps_memory_uncommitted(failure):
    from agent_components.models import ChatRequest

    memory = Memory()
    model = Backend()
    if failure == "provider":
        model.stream_error = RuntimeError("interrupted")
    if failure == "cancel":
        model.stream_error = asyncio.CancelledError()
    controller = ExecutionController(ExecutionPolicy(max_concurrency=1))
    client = ModelClient({"one": model}, default_alias="one", memory=memory, execution=controller)
    request = ChatRequest(messages=(user("生成中"),), session_id="test")
    async with aclosing(client.stream_chat(request)) as events:
        assert (await anext(events)).text == "完整回复"
        if failure != "close":
            with pytest.raises(RuntimeError if failure == "provider" else asyncio.CancelledError):
                await anext(events)
    assert model.closed and not memory.sessions
    assert await asyncio.wait_for(controller.run(lambda: asyncio.sleep(0, result="ok")), timeout=1)


async def test_memory_is_opt_in_and_bounds_history_to_full_turns():
    from agent_components.models import ChatRequest

    backend = Backend()
    memory = Memory()
    client = ModelClient({"one": backend}, default_alias="one", memory=memory, max_history_turns=1)
    for text in ("第一轮", "第二轮", "第三轮"):
        await client.chat(ChatRequest(messages=(user(text),), session_id="session"))
    assert backend.requests[-1].messages == (
        user("第二轮"),
        assistant("非流式回复"),
        user("第三轮"),
    )
    assert (await memory.load("session")).messages == (user("第三轮"), assistant("非流式回复"))
    await client.chat(ChatRequest(messages=(user("无状态"),)))
    assert backend.requests[-1].messages == (user("无状态"),)
    with pytest.raises(ValueError, match="one user"):
        await client.chat(ChatRequest(messages=(user("一"), user("二")), session_id="session"))


async def test_streamed_extraction_uses_same_route_and_system_prompt_then_validates():
    from agent_components.models import TextDelta

    default = Backend()
    extraction = Backend()
    extraction.stream_body = '{"amount": 32000}'
    client = ModelClient(
        {"chat": default, "extract": extraction},
        default_alias="chat",
        task_routes={"structured_extraction": "extract"},
        system_prompt="应用规则",
    )
    request = ModelRequest(
        messages=(system("只允许原文证据"), user("金额32000元")),
        schema={
            "type": "object",
            "properties": {"amount": {"type": "integer"}},
            "required": ["amount"],
            "additionalProperties": False,
        },
    )
    events = [event async for event in client.stream_generate(request)]
    assert [type(event) for event in events] == [TextDelta, ModelOutput]
    assert events[-1].data == {"amount": 32000}
    assert not default.requests
    messages = extraction.requests[-1].messages
    assert messages[-1] == request.messages[-1]
    assert "应用规则" in messages[0].text and "只允许原文证据" in messages[0].text
    assert '"amount"' in messages[0].text
    extraction.stream_body = '{"amount": "wrong"}'
    with pytest.raises(ValueError, match="schema"):
        assert [event async for event in client.stream_generate(request)]


async def test_chat_with_no_enabled_memory_does_not_issue_provider_call():
    from agent_components.models import ChatRequest

    backend = Backend()
    client = ModelClient({"one": backend}, default_alias="one")
    with pytest.raises(UnsupportedCapabilityError, match="memory"):
        await client.chat(ChatRequest(messages=(user("hello"),), session_id="missing"))
    assert not backend.requests


async def test_stream_deadline_applies_across_separate_consumer_tasks():
    from agent_components.models import ChatRequest

    backend = Backend()
    memory = Memory()
    controller = ExecutionController(ExecutionPolicy(timeout_seconds=0.05, max_concurrency=1))
    client = ModelClient({"one": backend}, default_alias="one", memory=memory, execution=controller)
    request = ChatRequest(messages=(user("生成中"),), session_id="test")
    async with aclosing(client.stream_chat(request)) as events:
        assert (await asyncio.wait_for(anext(events), timeout=1)).text == "完整回复"
        await asyncio.sleep(0.08)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(events), timeout=1)
    assert backend.closed and not memory.sessions
    assert await controller.run(lambda: asyncio.sleep(0, result="released")) == "released"


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
async def test_structured_stream_rejects_nonfinite_numbers(number):
    backend = Backend()
    backend.stream_body = '{"amount": ' + number + "}"
    client = ModelClient({"one": backend}, default_alias="one")
    request = ModelRequest(
        messages=(user("金额"),),
        schema={"type": "object", "properties": {"amount": {"type": "number"}}},
    )
    async with aclosing(client.stream_generate(request)) as events:
        assert (await anext(events)).text == backend.stream_body
        with pytest.raises(ValueError, match="finite"):
            await anext(events)


async def test_losing_concurrent_chat_returns_conflict_without_overwriting_winning_turn():
    from agent_components.models import ChatOutput, ChatRequest

    both_started = asyncio.Event()
    finish = asyncio.Event()

    class ConcurrentBackend(Backend):
        async def chat(self, request):
            self.requests.append(request)
            if len(self.requests) == 2:
                both_started.set()
            await finish.wait()
            return ChatOutput(text=request.messages[-1].text)

    model = ConcurrentBackend()
    memory = Memory()
    client = ModelClient({"one": model}, default_alias="one", memory=memory)
    first = asyncio.create_task(client.chat(ChatRequest(messages=(user("甲"),), session_id="same")))
    second = asyncio.create_task(
        client.chat(ChatRequest(messages=(user("乙"),), session_id="same"))
    )
    await asyncio.wait_for(both_started.wait(), timeout=1)
    finish.set()
    outputs = await asyncio.gather(first, second, return_exceptions=True)
    successes = [result for result in outputs if isinstance(result, ChatOutput)]
    assert len(successes) == 1
    assert sum(isinstance(result, RevisionConflictError) for result in outputs) == 1
    assert memory.sessions["same"].messages == (
        user(successes[0].text),
        assistant(successes[0].text),
    )
