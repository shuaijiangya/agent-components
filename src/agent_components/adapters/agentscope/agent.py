"""通过 AgentScope 原生循环运行独立的 Agent 请求。"""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from typing import Any
from uuid import uuid4

from agent_components.adapters.agentscope.events import RunEvents
from agent_components.adapters.agentscope.tools import create_toolkit, prepare_schemas
from agent_components.agent.contracts import (
    AgentConfig,
    AgentError,
    AgentEvent,
    AgentRequest,
    AgentResult,
    AgentTool,
    ToolContext,
)
from agent_components.core.contracts import RuntimeRef
from agent_components.core.errors import MissingDependencyError
from agent_components.execution import ExecutionController, ExecutionPolicy
from agent_components.models.call_logging import current_request_id, model_log_context


class AgentScopeAdapter:
    """装配原生 Agent、模型和 Toolkit，交付框架无关的流式及完整结果。

    每次调用分配新的 Agent 状态和工具组。调用方可传入历史消息，
    并负责注入模型的生命周期。组件对整个 Agent 运行不做重试。
    """

    def __init__(
        self,
        *,
        model: Any,
        config: AgentConfig | None = None,
        tools: Sequence[AgentTool[Any]] = (),
    ) -> None:
        """校验注入的原生模型与业务工具，并创建共享并发限制。

        Args:
            model: 已配置的 AgentScope ChatModelBase。
            config: 名称、系统指令、总超时、并发及原生循环上限。
            tools: 应用显式提供的业务工具列表。

        Raises:
            MissingDependencyError: 未安装 agent extra。
            TypeError: 模型不是 AgentScope ChatModelBase。
            ValueError: 工具配置无效。
        """
        try:
            from agentscope.model import ChatModelBase
        except ImportError as exc:
            raise MissingDependencyError("agent") from exc
        if not isinstance(model, ChatModelBase):
            raise TypeError("model must be an AgentScope ChatModelBase")
        self._model = model
        self._config = config or AgentConfig()
        self._tools = tuple(tools)
        self._schemas = prepare_schemas(self._tools)
        self._execution = ExecutionController(
            ExecutionPolicy(
                max_concurrency=self._config.max_concurrency,
                timeout_seconds=self._config.timeout_seconds,
            )
        )

    async def run(
        self, request: AgentRequest, *, context: ToolContext | None = None
    ) -> AgentResult:
        """消费完整事件流，返回同一份终态结果。"""
        async with aclosing(self.stream(request, context=context)) as events:
            async for event in events:
                if event.result is not None:
                    return event.result
        raise RuntimeError("Agent stream ended without a terminal result")

    async def stream(
        self, request: AgentRequest, *, context: ToolContext | None = None
    ) -> AsyncGenerator[AgentEvent, None]:
        """返回本次运行的公共事件，支持任务取消和主动关闭。"""
        runtime = RuntimeRef(id=request.run_id or uuid4().hex, backend="agentscope")
        recorder = RunEvents(
            runtime,
            self._schemas,
            request_id=request.request_id or current_request_id() or runtime.id,
        )
        tool_context = context.model_copy(deep=True) if context is not None else ToolContext()
        yield recorder.event("run.started")
        try:
            async with aclosing(
                self._execution.iterate(lambda: self._run_native(request, tool_context, recorder))
            ) as events:
                async for event in events:
                    yield event
        except TimeoutError as exc:
            yield recorder.finish(
                status="timed_out",
                error=AgentError(
                    code="agent_timeout",
                    message="The Agent run or an upstream dependency timed out.",
                    native_type=type(exc).__name__,
                ),
            )
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            yield recorder.finish(status="interrupted")
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            yield recorder.finish(
                status="failed",
                error=AgentError(
                    code="agent_execution_error",
                    message="The Agent run failed.",
                    native_type=type(exc).__name__,
                    http_status=status if type(status) is int else None,
                ),
            )
        else:
            yield recorder.finish()

    async def _run_native(
        self, request: AgentRequest, context: ToolContext, recorder: RunEvents
    ) -> AsyncGenerator[AgentEvent, None]:
        from agentscope.agent import Agent, InjectionConfig, ModelConfig, ReActConfig
        from agentscope.message import AssistantMsg, Msg, SystemMsg, UserMsg

        constructors = {"user": UserMsg, "assistant": AssistantMsg, "system": SystemMsg}
        messages = [
            constructors[message.role](name=message.role, content=message.text)
            for message in request.messages
        ]
        agent = Agent(
            name=self._config.name,
            system_prompt=self._config.system_prompt,
            model=self._model,
            toolkit=create_toolkit(self._tools, self._schemas, context),
            model_config=ModelConfig(max_retries=0),
            injection_config=InjectionConfig(inject_runtime_state=False),
            react_config=ReActConfig(
                max_iters=self._config.max_iterations, interruption_raise_cancelled_error=True
            ),
        )
        async with aclosing(agent.reply_stream(messages, yield_final_msg=True)) as stream:
            while True:
                # 不在 yield 期间保留 ContextVar token，使调用方可跨任务消费流。
                with model_log_context(run_id=recorder.runtime.id, request_id=recorder.request_id):
                    try:
                        native = await anext(stream)
                    except StopAsyncIteration:
                        return
                if isinstance(native, Msg):
                    recorder.message(native)
                elif event := recorder.convert(native):
                    yield event
