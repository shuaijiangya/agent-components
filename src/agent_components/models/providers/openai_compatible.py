"""连接显式配置的 OpenAI 兼容聊天与 Embedding 端点。"""

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Self

from pydantic import Field, SecretStr, model_validator

from agent_components.core.contracts import Contract
from agent_components.core.errors import MissingDependencyError
from agent_components.execution import ExecutionController, ExecutionPolicy
from agent_components.models.client import ModelClient
from agent_components.models.providers.agentscope import AgentScopeStructuredModel


class OpenAIModelEndpoint(Contract):
    """声明原生聊天模型的环境变量引用，端点地址由应用显式提供。"""

    model_env: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    base_url_env: str = Field(min_length=1)


class OpenAIModelSettings(Contract):
    """绑定 OpenAI 兼容端点、模型别名、任务规则和执行配置。"""

    models: dict[str, OpenAIModelEndpoint] = Field(min_length=1)
    default_alias: str
    task_routes: dict[str, str] = Field(default_factory=dict)
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)

    @model_validator(mode="after")
    def routes_exist(self) -> Self:
        """在打开客户端前校验默认和任务路由引用。"""
        if any(
            alias not in self.models for alias in (self.default_alias, *self.task_routes.values())
        ):
            raise ValueError("route refers to an unregistered alias")
        return self


@asynccontextmanager
async def openai_compatible_clients(
    settings: OpenAIModelSettings,
) -> AsyncIterator[tuple[ModelClient, dict[str, Any]]]:
    """创建模型客户端和按别名索引的 AgentScope 原生对象。

    统一客户端供结构化抽取使用。需要访问原生对象的应用可使用同一份别名注册。
    成功、异常或取消退出时都会在此作用域关闭客户端。

    Args:
        settings: 仅保存环境变量引用、模型路由和执行配置。

    Yields:
        模型客户端与当前作用域内的原生模型映射。

    Raises:
        KeyError: 配置引用的环境变量未提供。
        ValueError: 环境变量值为空。
        MissingDependencyError: 缺少 AgentScope 可选依赖。
    """
    # 先核对全部配置，在打开任何客户端之前发现缺失值。
    values = {}
    for alias, endpoint in settings.models.items():
        model = os.environ[endpoint.model_env]
        key = os.environ[endpoint.api_key_env]
        url = os.environ[endpoint.base_url_env]
        if not all((model, key, url)):
            raise ValueError("model environment variables must not be empty")
        values[alias] = (model, key, url)

    try:
        from agentscope.credential import OpenAICredential
        from agentscope.model import OpenAIChatModel
    except ImportError as exc:
        raise MissingDependencyError("agent") from exc

    native: dict[str, Any] = {}
    async with AsyncExitStack() as stack:
        for alias, (model, key, url) in values.items():
            instance = OpenAIChatModel(
                credential=OpenAICredential(api_key=SecretStr(key), base_url=url),
                model=model,
                stream=False,
                # 瞬时错误重试由执行器控制。结构化协议仍使用 AgentScope 原生兼容策略。
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


class OpenAICompatibleEmbeddings:
    """通过同步 OpenAI 兼容端点获取向量。

    dimension 应与部署模型输出维度一致。分块与检索组件按各自契约校验向量。
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        dimension: int,
    ) -> None:
        """连接指定端点并关闭 HTTP SDK 的隐式重试。"""
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise MissingDependencyError("agent") from exc

        self.dimension = dimension
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=30, max_retries=0)

    def embed_batch(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """批量请求向量并按响应索引恢复输入顺序。

        Args:
            texts: 待向量化的文本序列。

        Returns:
            与输入一一对应的向量。

        Raises:
            ValueError: 响应索引缺失、重复或越界。
        """
        response = self.client.embeddings.create(model=self.model, input=list(texts))
        rows = sorted(response.data, key=lambda row: row.index)
        if [row.index for row in rows] != list(range(len(texts))):
            raise ValueError("embedding response indices do not match inputs")
        return [row.embedding for row in rows]

    def close(self) -> None:
        """关闭同步 HTTP 客户端。"""
        self.client.close()
