import asyncio

import pytest

from agent_components.core.errors import RetryableError
from agent_components.execution import ExecutionController, ExecutionPolicy


async def test_only_explicit_transient_errors_are_retried():
    controller = ExecutionController(ExecutionPolicy(max_attempts=3, backoff_max_seconds=0.001))
    calls = 0

    async def transient():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RetryableError("temporary")
        return "ok"

    assert await controller.run(transient) == "ok"
    assert calls == 3


async def test_business_rejection_is_not_retried():
    controller = ExecutionController(ExecutionPolicy(max_attempts=3))
    calls = 0

    async def rejected():
        nonlocal calls
        calls += 1
        raise ValueError("business rejection")

    with pytest.raises(ValueError):
        await controller.run(rejected)
    assert calls == 1


async def test_deadline_includes_time_waiting_for_concurrency_slot():
    controller = ExecutionController(ExecutionPolicy(timeout_seconds=0.15, max_concurrency=1))
    entered = asyncio.Event()

    async def operation():
        entered.set()
        await asyncio.sleep(0.1)

    first = asyncio.create_task(controller.run(operation))
    await entered.wait()
    with pytest.raises(TimeoutError):
        await controller.run(operation)
    await first


async def test_cancellation_propagates_and_releases_slot():
    controller = ExecutionController(ExecutionPolicy(max_concurrency=1))
    entered = asyncio.Event()

    async def operation():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(controller.run(operation))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await controller.run(lambda: asyncio.sleep(0, result="released")) == "released"


async def test_stream_keeps_slot_until_provider_has_closed():
    controller = ExecutionController(ExecutionPolicy(max_concurrency=1))
    closing = asyncio.Event()
    cleaned = asyncio.Event()
    next_started = asyncio.Event()

    async def source():
        try:
            yield "partial"
        finally:
            closing.set()
            await cleaned.wait()

    async def next_operation():
        next_started.set()

    stream = controller.iterate(source)
    assert await anext(stream) == "partial"
    closing_task = asyncio.create_task(stream.aclose())
    await closing.wait()
    next_task = asyncio.create_task(controller.run(next_operation))
    try:
        await asyncio.sleep(0)
        assert not next_started.is_set()
    finally:
        cleaned.set()
        await closing_task
        await next_task
    assert next_started.is_set()
