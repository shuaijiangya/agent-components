"""统一管理异步调用的总期限、并发上限和显式重试。

总期限包含排队与退避，取消信号直接向调用方传播。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import Field
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from agent_components.core.contracts import Contract
from agent_components.core.errors import RetryableError

T = TypeVar("T")


class ExecutionPolicy(Contract):
    """配置一次逻辑调用的资源边界。

    max_attempts 默认一，不默认重试；max_concurrency 是当前控制器实例的上限。
    """

    timeout_seconds: float = Field(default=120.0, gt=0)
    max_concurrency: int = Field(default=4, ge=1)
    max_attempts: int = Field(default=1, ge=1)
    backoff_max_seconds: float = Field(default=2.0, gt=0)


class ExecutionController:
    """在实例范围内限制并发并执行总期限。

    只有 RetryableError 参与重试；不会推断工具写操作的幂等性。
    """

    def __init__(self, policy: ExecutionPolicy | None = None) -> None:
        """创建实例级信号量。

        Args:
            policy: 调用限制；省略时使用 ExecutionPolicy 默认值。
        """
        self.policy = policy or ExecutionPolicy()
        self._semaphore = asyncio.Semaphore(self.policy.max_concurrency)

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        # The deadline includes queueing, attempts and retry waits.
        """执行异步操作并应用排队、总超时和重试策略。

        Args:
            operation: 每次尝试都会重新调用的异步工厂，不是已创建的协程对象。

        Returns:
            operation 首次成功返回的结果。

        Raises:
            TimeoutError: 总期限耗尽。
            RetryableError: 已耗尽允许的尝试次数。

        Notes:
            其他操作异常及取消原样传播；持有并发槽期间的退避也计入总期限。
        """
        async with asyncio.timeout(self.policy.timeout_seconds):
            async with self._semaphore:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(self.policy.max_attempts),
                    wait=wait_random_exponential(max=self.policy.backoff_max_seconds),
                    retry=retry_if_exception_type(RetryableError),
                    reraise=True,
                ):
                    with attempt:
                        return await operation()
        raise RuntimeError("retry loop ended without an outcome")
