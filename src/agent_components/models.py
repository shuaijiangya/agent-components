"""实现进程内规则模型路由和无正文的调用日志。

供应商协议由 StructuredModel Adapter 负责，不部署独立网关。
"""

import json
import logging
import time
from collections.abc import Mapping
from uuid import uuid4

from agent_components.core.contracts import ModelOutput, ModelRequest
from agent_components.core.errors import RoutingError
from agent_components.core.ports import StructuredModel
from agent_components.execution import ExecutionController

logger = logging.getLogger("agent_components.model_calls")


class ModelClient:
    """根据别名、任务和默认配置选择模型。

    一次 generate 对应一条逻辑调用；执行器负责总超时、并发与可选重试。
    """

    def __init__(
        self,
        models: Mapping[str, StructuredModel],
        *,
        default_alias: str,
        task_routes: Mapping[str, str] | None = None,
        execution: ExecutionController | None = None,
    ) -> None:
        """保存模型注册和路由快照，并校验引用。

        Args:
            models: 别名到结构化模型后端的映射。
            default_alias: 无显式别名和任务规则时使用的模型。
            task_routes: 任务名称到模型别名的静态映射。
            execution: 可注入的并发、超时及重试控制器。

        Raises:
            RoutingError: 默认或任务规则引用了未注册的模型。
        """
        self._models = dict(models)
        self._default = default_alias
        self._routes = dict(task_routes or {})
        self._execution = execution or ExecutionController()
        unknown = ({default_alias} | set(self._routes.values())) - self._models.keys()
        if unknown:
            raise RoutingError(f"Unregistered model aliases: {sorted(unknown)}")

    async def generate(self, request: ModelRequest) -> ModelOutput:
        """执行一次路由后的结构化模型请求。

        Args:
            request: 消息、目标 Schema、任务和可选模型别名。

        Returns:
            后端原始结构化数据和用量；不在此执行业务 Schema 校验。

        Raises:
            RoutingError: 请求指定了未注册别名。
            TimeoutError: 排队、重试和执行总时长超过期限。

        Notes:
            日志记录请求标识、模型、时长、用量及异常类型，不记录正文或异常消息。
        """
        alias = request.model_alias or self._routes.get(request.task, self._default)
        if alias not in self._models:
            raise RoutingError(f"Unregistered model alias: {alias}")
        call_id = uuid4().hex
        started = time.monotonic()
        base = {
            "call_id": call_id,
            "request_id": request.request_id,
            "task": request.task,
            "model_alias": alias,
        }
        logger.info(json.dumps({**base, "event": "model.started"}))
        try:
            output = await self._execution.run(lambda: self._models[alias].generate(request))
        except BaseException as exc:
            # Error types only: provider messages can contain payloads or credentials.
            logger.info(
                json.dumps(
                    {
                        **base,
                        "event": "model.failed",
                        "error_type": type(exc).__name__,
                        "duration_seconds": time.monotonic() - started,
                    }
                )
            )
            raise
        logger.info(
            json.dumps(
                {
                    **base,
                    "event": "model.completed",
                    "model": output.model,
                    "duration_seconds": time.monotonic() - started,
                    "usage": output.usage.model_dump(),
                }
            )
        )
        return output
