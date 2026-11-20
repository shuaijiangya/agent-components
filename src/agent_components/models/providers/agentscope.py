"""适配 AgentScope 2.0.11 的模型级结构化输出。

不需要创建 Agent 实例，仅转换消息、输出和用量。
"""

from typing import Any, cast

from pydantic import JsonValue

from agent_components.core.contracts import ModelOutput, ModelRequest, Usage
from agent_components.core.errors import MissingDependencyError, UnsupportedCapabilityError


class AgentScopeStructuredModel:
    """封装原生支持结构化输出的模型对象。

    当前只支持 object 根 Schema；供应商异常保持原样传播。
    """

    def __init__(self, model: Any) -> None:
        """校验并绑定原生模型。

        Args:
            model: 提供 generate_structured_output 的原生模型。

        Raises:
            TypeError: 对象缺少可调用的结构化输出方法。
        """
        if not callable(getattr(model, "generate_structured_output", None)):
            raise TypeError("model must support generate_structured_output")
        self._model = model

    async def generate(self, request: ModelRequest) -> ModelOutput:
        """转换统一消息并请求原生结构化输出。

        Args:
            request: 消息及 object 根 JSON Schema。

        Returns:
            公共模型响应和供应商实际用量。

        Raises:
            UnsupportedCapabilityError: 根 Schema 不是 object。
            MissingDependencyError: 未安装 agent extra。
        """
        if request.schema_.get("type") != "object":
            raise UnsupportedCapabilityError(
                "AgentScope adapter currently requires object root schema"
            )
        try:
            from agentscope.message import AssistantMsg, SystemMsg, UserMsg
        except ImportError as exc:
            raise MissingDependencyError("agent") from exc
        constructors: dict[str, Any] = {
            "system": SystemMsg,
            "user": UserMsg,
            "assistant": AssistantMsg,
        }
        messages = [
            constructors[item.role](name=item.role, content=item.text) for item in request.messages
        ]
        response = await self._model.generate_structured_output(
            messages=messages,
            structured_model=request.schema_,
        )
        native_usage = response.usage
        usage = (
            Usage()
            if native_usage is None
            else Usage(
                input_tokens=native_usage.input_tokens,
                output_tokens=native_usage.output_tokens,
            )
        )
        return ModelOutput(
            data=cast(JsonValue, response.content),
            usage=usage,
            model=getattr(self._model, "model", None),
        )
