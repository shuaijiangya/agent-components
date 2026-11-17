"""装配已有模型路由组件与 AgentScope 原生连接。

配置只包含环境变量名称；不启动网关服务，不下载模型。
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, model_validator

from agent_components.core.contracts import Contract
from agent_components.execution import ExecutionController, ExecutionPolicy
from agent_components.models import ModelClient
from examples._common import read_json


class Endpoint(Contract):
    """声明一个模型端点的环境变量引用。

    base_url 必须显式配置，避免意外发往默认公网供应商。
    """

    model_env: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    base_url_env: str = Field(min_length=1)


class Settings(Contract):
    """绑定别名、任务路由和逻辑调用的资源限制。

    本示例只接入 OpenAI 兼容协议；其他协议可实现 SDK ModelBackend。
    """

    models: dict[str, Endpoint] = Field(min_length=1)
    default_alias: str
    task_routes: dict[str, str] = Field(default_factory=dict)
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)

    @model_validator(mode="after")
    def routes_exist(self) -> "Settings":
        """在建立连接之前检查全部路由目标。"""
        if any(
            alias not in self.models for alias in (self.default_alias, *self.task_routes.values())
        ):
            raise ValueError("route refers to an unregistered alias")
        return self


def configure_logs() -> None:
    """只为 SDK 模型事件启用 JSON 日志，保留业务正文的隔离边界。"""
    logger = logging.getLogger("agent_components.model_calls")
    logger.handlers = [logging.StreamHandler()]
    logger.setLevel(logging.INFO)
    logger.propagate = False


@asynccontextmanager
async def clients(path: Path) -> AsyncIterator[tuple[ModelClient, dict[str, Any]]]:
    """构造真实模型客户端，并在成功、异常和取消时关闭连接。

    Args:
        path: 只含环境变量引用和路由规则的 JSON 配置。

    Yields:
        公共 ModelClient 与按别名索引的原生 AgentScope 模型。

    Raises:
        KeyError: 运行环境缺少配置引用的变量。
        ValueError: 路由目标未注册或变量值为空。
    """
    settings = Settings.model_validate(read_json(path))
    # 先验证所有环境变量，避免只创建了一部分客户端才发现配置缺失。
    values = {}
    for alias, endpoint in settings.models.items():
        values[alias] = tuple(
            os.environ[name]
            for name in (endpoint.model_env, endpoint.api_key_env, endpoint.base_url_env)
        )
        if not all(values[alias]):
            raise ValueError("model environment variables must not be empty")
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel

    from agent_components.adapters.agentscope.model import AgentScopeStructuredModel

    native = {}
    async with AsyncExitStack() as stack:
        for alias, (model, key, url) in values.items():
            instance = OpenAIChatModel(
                credential=OpenAICredential(api_key=SecretStr(key), base_url=url),
                model=model,
                stream=False,
                # 关闭两层瞬时错误重试；原生结构化输出的兼容策略 fallback 仍存在。
                max_retries=0,
                client_kwargs={"max_retries": 0, "timeout": settings.execution.timeout_seconds},
            )
            stack.push_async_callback(instance.client.close)
            native[alias] = instance
        yield (
            ModelClient(
                {alias: AgentScopeStructuredModel(model) for alias, model in native.items()},
                default_alias=settings.default_alias,
                task_routes=settings.task_routes,
                execution=ExecutionController(settings.execution),
            ),
            native,
        )
