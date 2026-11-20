"""提供模型调用事件的日志通道，日志落点由宿主配置。"""

import json
import logging
from collections.abc import Mapping

from pydantic import JsonValue

MODEL_CALL_LOGGER_NAME = "agent_components.model_calls"
_logger = logging.getLogger(MODEL_CALL_LOGGER_NAME)


def _emit_call_event(event: str, context: Mapping[str, str | None], **details: JsonValue) -> None:
    """用固定日志通道写入一条 JSON 事件。"""
    _logger.info(json.dumps({**context, "event": event, **details}))
