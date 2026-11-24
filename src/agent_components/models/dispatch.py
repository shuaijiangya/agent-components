"""在统一的并发槽、期限和尝试次数内执行模型路由。"""

import asyncio
import json
import random
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing

from agent_components.core.contracts import ModelOutput, ModelRequest, Usage
from agent_components.core.errors import RetryableError, UnsupportedCapabilityError
from agent_components.execution import ExecutionController
from agent_components.models.call_logging import ModelCall
from agent_components.models.contracts import ChatOutput, ChatRequest, ModelBackend, TextDelta
from agent_components.models.routing import _ModelRouter

type Output = ChatOutput | ModelOutput | TextDelta
type Request = ChatRequest | ModelRequest
type Candidate = tuple[str, ModelBackend]


def _input_size(request: Request) -> int:
    """默认按消息的 UTF-8 字节、角色、Schema 和少量协议开销保守估算。"""
    if request.input_tokens is not None:
        return request.input_tokens
    size = 3 + sum(4 + len(msg.role) + len(msg.text.encode("utf-8")) for msg in request.messages)
    if isinstance(request, ModelRequest):
        size += len(json.dumps(request.schema_, ensure_ascii=False).encode("utf-8"))
    return size


def _transient(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status in (408, 409, 429) or 500 <= status < 600
    if isinstance(error, (RetryableError, ConnectionError, TimeoutError)):
        return True
    # 核心包可独立安装。按 SDK 异常所属模块匹配，避免导入可选模型依赖。
    return any(
        base.__module__.startswith("openai") and base.__name__ == "APIConnectionError"
        for base in type(error).__mro__
    )


def _can_fallback(error: Exception) -> bool:
    return (
        _transient(error)
        or isinstance(error, UnsupportedCapabilityError)
        or getattr(error, "status_code", None) == 404
        or any(
            base.__module__.startswith("agentscope") and base.__name__ == "StructuredOutputError"
            for base in type(error).__mro__
        )
    )


async def _attempts[T: Output](
    candidates: Sequence[Candidate],
    execution: ExecutionController,
    call: ModelCall,
    invoke: Callable[[ModelBackend], AsyncGenerator[T, None]],
) -> AsyncGenerator[T, None]:
    policy = execution.policy
    index = 0
    for number in range(1, policy.max_attempts + 1):
        alias, backend = candidates[index]
        exposed = False
        usage: Usage | None = None
        try:
            with call.attempt(alias, model=getattr(backend, "model_name", None)) as attempt:
                async with aclosing(invoke(backend)) as responses:
                    while True:
                        try:
                            # ContextVar 只在读取供应商数据时有效。调用方可以跨任务读取同一条流。
                            with attempt.bind():
                                output = await anext(responses)
                        except StopAsyncIteration:
                            break
                        if output.usage is not None:
                            usage = output.usage
                        exposed = True
                        yield output
                attempt.complete(usage)
            return
        except Exception as exc:
            if exposed or number == policy.max_attempts:
                raise
            if index + 1 < len(candidates) and _can_fallback(exc):
                index += 1
                call.event(
                    "model.fallback",
                    from_alias=alias,
                    to_alias=candidates[index][0],
                    error_type=type(exc).__name__,
                    attempt_number=number,
                )
            elif _transient(exc):
                call.event(
                    "model.retry",
                    model_alias=alias,
                    error_type=type(exc).__name__,
                    attempt_number=number,
                )
                delay = min(policy.backoff_max_seconds, 2.0 ** min(number - 1, 30))
                await asyncio.sleep(random.uniform(0, delay))
            else:
                raise


def dispatch[T: Output](
    router: _ModelRouter,
    execution: ExecutionController,
    request: Request,
    operation: str,
    call: ModelCall,
    invoke: Callable[[ModelBackend], AsyncGenerator[T, None]],
) -> AsyncGenerator[T, None]:
    """计划候选模型并在同一执行期限内消费所有尝试，包括退避等待。"""
    tokens = _input_size(request)
    candidates = router.candidates(request, operation=operation, input_tokens=tokens)
    preferred, rule = router.preferred(request)
    alias = candidates[0][0]
    call.event(
        "model.route",
        model_alias=alias,
        reason=rule if alias == preferred else "capability_or_size",
        input_tokens=tokens,
        estimated=request.input_tokens is None,
        candidates=[key for key, _ in candidates],
    )
    return execution.iterate(lambda: _attempts(candidates, execution, call, invoke))
