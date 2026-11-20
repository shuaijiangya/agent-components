"""读取示例配置并装配模型包提供的客户端。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agent_components.models import MODEL_CALL_LOGGER_NAME, ModelClient
from agent_components.models.providers.openai_compatible import (
    OpenAIModelSettings,
    openai_compatible_clients,
)
from examples._common import read_json


def configure_logs() -> None:
    """将 SDK 模型调用事件写入命令行的 stderr。"""
    logger = logging.getLogger(MODEL_CALL_LOGGER_NAME)
    logger.handlers = [logging.StreamHandler()]
    logger.setLevel(logging.INFO)
    logger.propagate = False


@asynccontextmanager
async def clients(path: Path) -> AsyncIterator[tuple[ModelClient, dict[str, Any]]]:
    """读取示例 JSON，并在 SDK 管理的连接作用域内交付模型。"""
    settings = OpenAIModelSettings.model_validate(read_json(path))
    async with openai_compatible_clients(settings) as models:
        yield models
