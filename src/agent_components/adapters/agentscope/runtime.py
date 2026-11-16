"""转换 AgentScope 的上下文和权限能力。

使用原生运行对象，不实现第二套 Agent 循环或审批流程。
"""

from typing import Any, cast

from pydantic import JsonValue

from agent_components.core.contracts import PermissionResult, RuntimeRef, StateEnvelope
from agent_components.core.errors import UnsupportedCapabilityError


class AgentScopeContextAdapter:
    """将原生上下文状态包装为公共契约。

    每个 Adapter 绑定一个运行实例；压缩行为由原生 Agent 决定。
    """

    def __init__(self, runtime: RuntimeRef, agent: Any) -> None:
        """绑定运行标识和原生 Agent。

        Args:
            runtime: backend 必须为 agentscope 的运行标识。
            agent: 提供 state 和 compress_context 的原生对象。

        Raises:
            ValueError: backend 不匹配。
        """
        if runtime.backend != "agentscope":
            raise ValueError("runtime backend must be agentscope")
        self._runtime = runtime
        self._agent = agent

    def _check(self, runtime: RuntimeRef) -> None:
        if runtime != self._runtime:
            raise ValueError("runtime reference does not belong to this adapter")

    async def snapshot(self, runtime: RuntimeRef) -> StateEnvelope:
        """序列化当前原生状态，不写入数据库。

        Args:
            runtime: 必须与绑定的运行标识一致。

        Returns:
            AgentScope 2.0.11 版本的状态封套。

        Raises:
            ValueError: runtime 不属于当前 Adapter。
        """
        self._check(runtime)
        return StateEnvelope(
            backend="agentscope",
            schema_version="2.0.11",
            payload=cast(dict[str, JsonValue], self._agent.state.model_dump(mode="json")),
        )

    async def compact(self, runtime: RuntimeRef) -> StateEnvelope:
        """调用原生上下文压缩并获取快照。

        Args:
            runtime: 必须与绑定的运行标识一致。

        Returns:
            原生压缩完成后的状态封套。

        Notes:
            是否调用模型及如何摘要由原生配置决定，组件不自动持久化。
        """
        self._check(runtime)
        await self._agent.compress_context()
        return await self.snapshot(runtime)


class AgentScopePermissionAdapter:
    """将原生权限行为转换为 allow、deny 或 pending。

    pending 只代表需要确认，不包含确认页面、持久化或恢复。
    """

    def __init__(self, engine: Any) -> None:
        """注入原生权限引擎。

        Args:
            engine: 提供异步 check_permission 的原生对象。
        """
        self._engine = engine

    async def check(self, tool: Any, parameters: dict[str, JsonValue]) -> PermissionResult:
        """查询原生权限判定，不执行工具。

        Args:
            tool: 原生工具对象。
            parameters: 本次工具调用参数。

        Returns:
            转换后的权限结果和原生理由。

        Raises:
            UnsupportedCapabilityError: 后端返回未知权限行为。
        """
        native = await self._engine.check_permission(tool, parameters)
        value = native.behavior.value.lower()
        mapping = {"allow": "allow", "deny": "deny", "ask": "pending"}
        if value not in mapping:
            raise UnsupportedCapabilityError(f"Unknown native permission behavior: {value}")
        return PermissionResult(status=mapping[value], reason=native.message or "")  # type: ignore[arg-type]
