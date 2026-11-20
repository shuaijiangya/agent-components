"""按显式别名、任务配置和默认别名确定模型调用目标。"""

from collections.abc import Mapping

from agent_components.core.contracts import ModelRequest
from agent_components.core.errors import RoutingError
from agent_components.models.contracts import ChatRequest, ModelBackend


class _ModelRouter:
    """保存路由快照并校验配置中的模型引用。"""

    def __init__(
        self,
        models: Mapping[str, ModelBackend],
        default_alias: str,
        task_routes: Mapping[str, str] | None,
    ) -> None:
        self._models = dict(models)
        self._default = default_alias
        self._routes = dict(task_routes or {})
        unknown = ({default_alias} | set(self._routes.values())) - self._models.keys()
        if unknown:
            raise RoutingError(f"Unregistered model aliases: {sorted(unknown)}")

    def resolve(self, request: ModelRequest | ChatRequest) -> tuple[str, ModelBackend]:
        """解析本次调用的模型别名和已注册的后端。"""
        alias = request.model_alias or self._routes.get(request.task, self._default)
        if alias not in self._models:
            raise RoutingError(f"Unregistered model alias: {alias}")
        return alias, self._models[alias]
