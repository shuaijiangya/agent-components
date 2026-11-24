"""桥接 LangExtract 同步 Provider 与统一异步模型入口。

任意业务 Schema 到 LangExtract 字段证据的映射不在此桥接层中实现。
"""

import asyncio
import json
from collections.abc import Iterator, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, cast

from pydantic import JsonValue

from agent_components.core.contracts import Message, ModelRequest
from agent_components.core.errors import MissingDependencyError
from agent_components.core.ports import StructuredModel


def create_provider(
    client: StructuredModel,
    *,
    loop: asyncio.AbstractEventLoop,
    output_schema: dict[str, JsonValue],
    model_alias: str | None = None,
    timeout_seconds: float = 120,
    request_id: str | None = None,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
) -> Any:
    """创建调用统一模型入口的原生 LangExtract Provider。

    Args:
        client: 包含路由、日志和执行策略的模型客户端。
        loop: 正在运行的模型所属事件循环。
        output_schema: LangExtract extraction envelope 的 Schema，不是任意业务结构。
        model_alias: 可选的显式模型别名。
        timeout_seconds: 单次同步等待的上限，必须为正数。
        request_id: 在多条 LangExtract 提示之间关联同一抽取请求。
        system_prompt: 单次抽取的系统提示覆盖。
        max_tokens: 各分段的最大输出 Token 数。

    Returns:
        原生 BaseLanguageModel 实例；infer 必须在所属事件循环之外调用。

    Raises:
        ValueError: 超时配置非正。
        MissingDependencyError: 未安装 extraction extra。

    Notes:
        可通过 asyncio.to_thread 运行 LangExtract，避免阻塞模型事件循环。
    """
    try:
        from langextract.core.base_model import BaseLanguageModel
        from langextract.core.types import ScoredOutput
        from langextract.providers.schemas.openai import OpenAISchema
    except ImportError as exc:
        raise MissingDependencyError("extraction") from exc
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    class Provider(BaseLanguageModel):  # type: ignore[misc]
        """把原生批量推理同步转交到已绑定的异步事件循环。

        输出保持 LangExtract ScoredOutput 结构，不在此层猜测字段证据。
        """

        @classmethod
        def get_schema_class(cls) -> type[Any]:
            """使用原生的 JSON 封套 Schema，模型请求仍由统一客户端负责。"""
            return cast(type[Any], OpenAISchema)

        def infer(self, batch_prompts: Sequence[str], **kwargs: Any) -> Iterator[Any]:
            """逐个请求模型并产出原生评分输出。

            Args:
                batch_prompts: LangExtract 生成的提示序列。
                **kwargs: 原生 Provider 协议参数，当前不用于路由。

            Yields:
                单条提示对应的 ScoredOutput 列表。

            Raises:
                RuntimeError: 在所属事件循环内调用或该循环未运行。
                TimeoutError: 等待超时，已尝试取消对应异步请求。
            """
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is loop:
                raise RuntimeError(
                    "Run LangExtract via asyncio.to_thread; do not block its model loop"
                )
            if not loop.is_running():
                raise RuntimeError("The configured model event loop is not running")
            for prompt in batch_prompts:
                future = asyncio.run_coroutine_threadsafe(
                    client.generate(
                        ModelRequest(
                            messages=(Message(role="user", text=prompt),),
                            schema=output_schema,
                            task="document_extraction",
                            model_alias=model_alias,
                            request_id=request_id,
                            system_prompt=system_prompt,
                            max_tokens=max_tokens,
                        )
                    ),
                    loop,
                )
                try:
                    response = future.result(timeout=timeout_seconds)
                except FutureTimeoutError:
                    future.cancel()
                    raise TimeoutError("LangExtract model request exceeded deadline") from None
                yield [ScoredOutput(output=json.dumps(response.data, ensure_ascii=False))]

    return Provider()
