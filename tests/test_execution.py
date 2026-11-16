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
