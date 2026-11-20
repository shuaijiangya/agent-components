# 模型调用组件

应用可注入已创建的模型，或用模型包的 OpenAI 兼容工厂装配 AgentScope 客户端。聊天和结构化抽取共用 `ModelClient` 的路由、系统提示词和调用日志。聊天支持应用注入会话历史。Embedding 与重排可分别注入分块和检索。

| 入口 | 职责 |
| --- | --- |
| [client.py](client.py) | 聊天与结构化流式／非流式入口，组合消息、选模、执行控制和请求级事件 |
| [contracts.py](contracts.py) | 聊天请求、完整响应、文本增量及相应后端接口 |
| [memory.py](memory.py) | 会话历史协议和基于版本化状态存储的持久适配 |
| [routing.py](routing.py) | 显式模型别名、任务映射、默认模型与别名校验 |
| [call_logging.py](call_logging.py) | 通过 `agent_components.model_calls` 输出 JSON 事件，由应用设置日志落点和保留周期 |
| [providers/agentscope.py](providers/agentscope.py) | AgentScope 结构化输出、完整聊天和 OpenAI 兼容 SSE |
| [providers/openai_compatible.py](providers/openai_compatible.py) | 聊天模型端点配置与连接生命周期、同步 Embedding 客户端 |
| [providers/langextract.py](providers/langextract.py) | 把 LangExtract 的同步模型请求转交给 `ModelClient` |
| [providers/reranking.py](providers/reranking.py) | 重排 Provider 协议与 Cohere 兼容 HTTP 适配器 |

## 配置端点并调用

在仓库根目录准备 `agent` 依赖，并为配置文件中的别名设置模型名、基础 URL 和密钥。配置文件保存环境变量名称。[示例配置及环境说明](../../../docs/examples/models.md#显式配置端点与策略)

```python
import asyncio
from pathlib import Path

from agent_components.models import Message, ModelRequest
from agent_components.models.providers.openai_compatible import (
    OpenAIModelSettings,
    openai_compatible_clients,
)


async def main() -> None:
    settings = OpenAIModelSettings.model_validate_json(
        Path("examples/data/models.json").read_bytes()
    )
    request = ModelRequest(
        messages=(Message(role="user", text="提取合同金额。合同总金额为 32000 元。"),),
        schema={
            "type": "object",
            "properties": {"amount_cny": {"type": "integer"}},
            "required": ["amount_cny"],
        },
        task="structured_extraction",
        request_id="contract-review-001",
    )
    async with openai_compatible_clients(settings) as (client, _):
        result = await client.generate(request)
        print(result.data, result.usage)


if __name__ == "__main__":
    asyncio.run(main())
```

已有模型实例时可以实现 `StructuredModel.generate` 并按别名注册到 `ModelClient`。路由顺序为显式别名、任务规则、默认别名。构造时核验任务规则与默认别名，调用时校验显式引用。[模型接口与示例](../../../docs/examples/models.md)

普通聊天、结构化抽取、Embedding 和重排的流式与非流式示例可在同一份脚本中对照。[接入命令和输出约定](../../../docs/examples/siliconflow-models.md#四类调用的流式和非流式示例) · [Python 源码](../../../examples/siliconflow_modes.py)

## 系统提示词和会话历史

系统提示词可在 `OpenAIModelSettings.system_prompt` 或 `ModelClient(system_prompt=...)` 设置。单次请求的 `system_prompt` 可以覆盖默认值。调用方已放在消息开头的系统指令保留，并与配置指令合并，排在历史和本轮消息之前。默认指令和单次指令都可应用于聊天及抽取。

需要跨请求保留聊天历史时，注入 `ConversationMemory` 并传入 `ChatRequest.session_id`。`StateConversationMemory` 可绑定现有 `SQLiteStateStore`。业务服务应在鉴权后提供会话 ID，并按租户配置命名空间。会话模式每次提交一条本轮用户消息和可选的系统指令。SDK 读取最近 `max_history_turns` 轮完整对话，默认 12 轮。

下例接续前面的端点配置。会话持久化使用 `storage` 依赖组，示例显式创建开发表。生产使用现有状态存储迁移。

```python
from contextlib import aclosing
from pathlib import Path

from agent_components.models import ChatRequest, Message, StateConversationMemory, TextDelta
from agent_components.models.providers.openai_compatible import (
    OpenAIModelSettings,
    openai_compatible_clients,
)
from agent_components.storage.sqlite import SQLiteStateStore


async def chat_with_memory(settings: OpenAIModelSettings) -> None:
    async with SQLiteStateStore(Path("data/memory.sqlite"), create_tables=True) as store:
        memory = StateConversationMemory(store, namespace="contract-tenant")
        async with openai_compatible_clients(settings, memory=memory) as (client, _):
            await client.chat(
                ChatRequest(
                    messages=(Message(role="user", text="合同尾款应在验收后30日内支付。"),),
                    session_id="contract-review-001",
                    system_prompt="仅依据会话中的合同内容回答。",
                )
            )
            reply = ChatRequest(
                messages=(Message(role="user", text="尾款支付期限是多久？"),),
                session_id="contract-review-001",
                system_prompt="仅依据会话中的合同内容回答。",
            )
            async with aclosing(client.stream_chat(reply)) as events:
                async for event in events:
                    if isinstance(event, TextDelta):
                        print(event.text, end="", flush=True)
                    else:
                        print("\n完整回复", event.text)
```

会话库记录成功完成的用户和助手消息对。流中取消、截断或提前关闭时保留已提交的历史。并发写入使用版本比较，遇到 `RevisionConflictError` 时由业务方重新读取会话后决定是否再发起请求。结构化请求可用 `generate` 或 `stream_generate`；两者应用相同的任务路由。流式结构化调用先输出 `TextDelta`，流完成且符合 JSON Schema 后返回 `ModelOutput`。Pydantic 自定义校验可在调用方执行。

## 调用日志与兼容入口

日志提供逻辑调用的开始、完成和失败事件，记录请求 ID、调用 ID、任务、模型别名、耗时和已知用量。启用 `MODEL_CALL_LOGGER_NAME` 对应的 Python logger 接收事件。AgentScope 原生结构化协议可在同一次逻辑调用中尝试多种策略，实际 HTTP 次数由供应商请求遥测核对。[重试与调用次数](../../../docs/examples/models.md#日志重试与调用次数)

原有的 `agent_components.models.ModelClient`、`agent_components.adapters.agentscope.model`、`agent_components.adapters.langextract` 和 `agent_components.retrieval.reranking` 仍可导入。现有分块与检索可以继续使用自己的 Provider 协议。
