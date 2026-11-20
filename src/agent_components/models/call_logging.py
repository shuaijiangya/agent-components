"""提供模型调用事件的日志通道，日志落点由宿主配置。"""

import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from uuid import uuid4

from pydantic import JsonValue

from agent_components.core.contracts import ModelOutput, ModelRequest
from agent_components.models.contracts import ChatOutput, ChatRequest

MODEL_CALL_LOGGER_NAME = "agent_components.model_calls"
_logger = logging.getLogger(MODEL_CALL_LOGGER_NAME)


def _emit_call_event(event: str, context: Mapping[str, str | None], **details: JsonValue) -> None:
    """用固定日志通道写入一条 JSON 事件。"""
    _logger.info(json.dumps({**context, "event": event, **details}))


@contextmanager
def _model_call(
    request: ModelRequest | ChatRequest, alias: str
) -> Iterator[Callable[[ModelOutput | ChatOutput], None]]:
    """为逻辑调用关联开始、正常完成与异常事件，只记录安全元数据。"""
    context = {
        "call_id": uuid4().hex,
        "request_id": request.request_id,
        "task": request.task,
        "model_alias": alias,
    }
    started = time.monotonic()
    _emit_call_event("model.started", context)

    def complete(output: ModelOutput | ChatOutput) -> None:
        _emit_call_event(
            "model.completed",
            context,
            model=output.model,
            duration_seconds=time.monotonic() - started,
            usage=output.usage.model_dump(),
        )

    try:
        yield complete
    except BaseException as exc:
        _emit_call_event(
            "model.failed",
            context,
            error_type=type(exc).__name__,
            duration_seconds=time.monotonic() - started,
        )
        raise
