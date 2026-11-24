import asyncio
import json
import logging
from contextlib import aclosing

import pytest

from agent_components.core.errors import RetryableError
from agent_components.execution import ExecutionController, ExecutionPolicy
from agent_components.models import ChatOutput, ChatRequest, Message, ModelClient, TextDelta


def request():
    return ChatRequest(messages=(Message(role="user", text="hello"),))


class Endpoint:
    def __init__(self, error=None, first=""):
        self.error = error
        self.first = first
        self.calls = 0

    async def chat(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return ChatOutput(text="answer")

    async def stream_chat(self, request, *, json_mode=False):
        self.calls += 1
        if self.first:
            yield TextDelta(text=self.first)
        if self.error:
            raise self.error
        yield TextDelta(text="answer")


def client(primary, backup, *, limit):
    return ModelClient(
        {"p": primary, "b": backup},
        default_alias="p",
        fallback_routes={"p": ["b"]},
        execution=ExecutionController(
            ExecutionPolicy(max_attempts=limit, backoff_max_seconds=0.001)
        ),
    )


@pytest.mark.parametrize("streaming", [True, False])
async def test_retryable_failure_changes_alias_and_correlates_attempts(streaming, caplog):
    main, spare = Endpoint(RetryableError()), Endpoint()
    model = client(main, spare, limit=2)
    with caplog.at_level(logging.INFO, logger="agent_components.model_calls"):
        if streaming:
            outputs = [item async for item in model.stream_chat(request())]
            assert outputs[-1].text == "answer"
        else:
            assert (await model.chat(request())).text == "answer"
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "agent_components.model_calls"
    ]
    starts = [event for event in events if event["event"] == "model.attempt.started"]
    assert [event["model_alias"] for event in starts] == ["p", "b"]
    assert len({event["attempt_id"] for event in starts}) == 2
    assert len({event["call_id"] for event in events}) == 1
    assert any(event["event"] == "model.fallback" for event in events)
    assert (main.calls, spare.calls) == (1, 1)


@pytest.mark.parametrize("error", [ValueError("invalid input"), asyncio.CancelledError()])
async def test_invalid_input_and_cancellation_do_not_retry(error):
    primary, backup = Endpoint(error), Endpoint()
    with pytest.raises(type(error)):
        await client(primary, backup, limit=3).chat(request())
    assert (primary.calls, backup.calls) == (1, 0)


async def test_partial_stream_never_switches_models():
    primary, backup = Endpoint(RetryableError(), first="partial"), Endpoint()
    async with aclosing(client(primary, backup, limit=2).stream_chat(request())) as stream:
        assert (await anext(stream)).text == "partial"
        with pytest.raises(RetryableError):
            await anext(stream)
    assert (primary.calls, backup.calls) == (1, 0)


async def test_one_attempt_budget_is_shared_across_fallbacks():
    primary, backup = Endpoint(RetryableError()), Endpoint()
    with pytest.raises(RetryableError):
        await client(primary, backup, limit=1).chat(request())
    assert (primary.calls, backup.calls) == (1, 0)
