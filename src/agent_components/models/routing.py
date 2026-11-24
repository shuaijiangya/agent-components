"""根据任务、显式别名、能力与上下文窗口选择模型并规划有序回退。"""

from collections.abc import Mapping, Sequence

from pydantic import Field

from agent_components.core.contracts import Contract, ModelRequest
from agent_components.core.errors import RoutingError, UnsupportedCapabilityError
from agent_components.models.contracts import ChatRequest, ModelBackend

_OPERATIONS = {
    "generate": ("generate", "structured output"),
    "chat": ("chat", "chat"),
    "stream_chat": ("stream_chat", "streaming"),
    "stream_generate": ("stream_chat", "streaming"),
}


class ModelProfile(Contract):
    """描述模型别名可供路由匹配的额外能力和上下文容量。

    Attributes:
        capabilities: 业务自定义的能力标签，按完整字符串匹配。操作本身另查后端方法。
        context_window_tokens: 输入与预留输出的 Token 总容量。为 None 时不限制容量。
    """

    capabilities: tuple[str, ...] = ()
    context_window_tokens: int | None = Field(default=None, gt=0, strict=True)


class _ModelRouter:
    """保存注册顺序和配置快照，在任何供应商调用之前生成候选序列。"""

    def __init__(
        self,
        models: Mapping[str, ModelBackend],
        default_alias: str,
        task_routes: Mapping[str, str] | None,
        profiles: Mapping[str, ModelProfile] | None = None,
        fallback_routes: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        """校验引用并复制列表，确保运行期间候选顺序稳定。"""
        self._models = dict(models)
        self._default = default_alias
        self._routes = dict(task_routes or {})
        self._fallbacks: dict[str, tuple[str, ...]] = {}
        referenced = {default_alias, *self._routes.values(), *(profiles or {})}
        for alias, fallbacks in (fallback_routes or {}).items():
            if not isinstance(fallbacks, Sequence) or isinstance(fallbacks, (str, bytes)):
                raise RoutingError(f"Fallback route for {alias} must be a sequence of aliases")
            # 每条回退路线为扁平序列，同一别名最多尝试一次。
            self._fallbacks[alias] = tuple(dict.fromkeys(a for a in fallbacks if a != alias))
            referenced.update((alias, *fallbacks))
        unknown = referenced - self._models.keys()
        if unknown:
            raise RoutingError(f"Unregistered model aliases: {sorted(unknown)}")
        profiles = profiles or {}
        default_profile = ModelProfile()
        self._profiles = {
            alias: ModelProfile.model_validate(profiles.get(alias, default_profile))
            for alias in self._models
        }

    def preferred(self, request: ModelRequest | ChatRequest) -> tuple[str, str]:
        """返回能力匹配前的别名及选路依据，便于记录调用。"""
        if request.model_alias is not None:
            return request.model_alias, "explicit_alias"
        if request.task in self._routes:
            return self._routes[request.task], "task_rule"
        return self._default, "default"

    def resolve(self, request: ModelRequest | ChatRequest) -> tuple[str, ModelBackend]:
        """按显式别名、任务规则或默认值读取首选后端。"""
        alias, _ = self.preferred(request)
        if alias not in self._models:
            raise RoutingError(f"Unregistered model alias: {alias}")
        return alias, self._models[alias]

    def candidates(
        self, request: ModelRequest | ChatRequest, *, operation: str, input_tokens: int
    ) -> list[tuple[str, ModelBackend]]:
        """为本次调用返回可用的首选后端及按配置排序的故障回退。

        显式别名必须能够接收本次调用。使用任务或默认路线时，先检查首选别名及
        其配置的回退，都不适用时再检查注册表，已知窗口优先，其次按注册顺序。
        运行时沿所选路线的平坦回退列表依次尝试。

        Args:
            request: 已应用本次输出上限与所需能力的请求。
            operation: chat、generate、stream_chat 或 stream_generate。
            input_tokens: 完整输入的 Token 数，或客户端使用的估算值。

        Returns:
            首项为初选结果，后续项可在发生符合策略的失败后依次调用。

        Raises:
            RoutingError: 别名未知、操作无效或上下文容量不足。
            UnsupportedCapabilityError: 所选路线的后端缺少所需能力。
        """
        preferred, _ = self.resolve(request)
        if operation not in _OPERATIONS:
            raise RoutingError(f"Unknown model operation: {operation}")
        method, description = _OPERATIONS[operation]
        needed = input_tokens + (request.max_tokens or 0)
        required = set(request.required_capabilities)

        def incompatibility(alias: str) -> UnsupportedCapabilityError | RoutingError | None:
            profile = self._profiles[alias]
            backend = self._models[alias]
            supports = getattr(backend, "supports_operation", None)
            if not callable(getattr(backend, method, None)) or (
                callable(supports) and not supports(operation)
            ):
                return UnsupportedCapabilityError(f"Selected model does not support {description}")
            missing = required.difference(profile.capabilities)
            if missing:
                return UnsupportedCapabilityError(
                    f"Model {alias} lacks capabilities: {sorted(missing)}"
                )
            window = profile.context_window_tokens
            if window is not None and needed > window:
                return RoutingError(
                    f"Model {alias} context window is {window} tokens; need {needed}"
                )
            return None

        def fallbacks(alias: str) -> list[tuple[str, ModelBackend]]:
            return [
                (other, self._models[other])
                for other in self._fallbacks.get(alias, ())
                if incompatibility(other) is None
            ]

        problem = incompatibility(preferred)
        if problem is None:
            return [(preferred, self._models[preferred]), *fallbacks(preferred)]
        if request.model_alias is not None:
            raise problem
        configured = fallbacks(preferred)
        if configured:
            return configured
        tried = {preferred, *self._fallbacks.get(preferred, ())}
        by_window = sorted(
            self._profiles, key=lambda a: self._profiles[a].context_window_tokens is None
        )
        for alias in by_window:
            if alias not in tried and incompatibility(alias) is None:
                return [(alias, self._models[alias]), *fallbacks(alias)]
        raise problem
