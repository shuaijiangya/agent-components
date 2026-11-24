# 模型调用组件

应用可注入已创建的模型，或用模型包的 OpenAI 兼容工厂装配 AgentScope 客户端。聊天和结构化抽取共用 `ModelClient` 的路由、系统提示词和调用日志。聊天支持应用注入会话历史。Embedding 与重排可分别注入分块和检索。

| 入口 | 职责 |
| --- | --- |
| [client.py](client.py) | 聊天与结构化流式／非流式入口，组合消息、选模、执行控制和请求级事件 |
| [contracts.py](contracts.py) | 聊天请求、完整响应、文本增量及相应后端接口 |
| [memory.py](memory.py) | 会话历史协议和基于版本化状态存储的持久适配 |
| [routing.py](routing.py) | 别名和任务优先级、能力标签、上下文容量及备用模型顺序 |
| [call_logging.py](call_logging.py) | 逻辑调用、供应商尝试和 HTTP 发送事件，以及标准输出或滚动 JSONL 配置 |
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

## 按能力和上下文选模

下面的配置适用于两个具备结构化输出能力的 OpenAI 兼容端点。将窗口容量改为实际部署值。

```json
{
  "models": {
    "standard": {
      "model_env": "GENERAL_MODEL", "api_key_env": "GENERAL_API_KEY",
      "base_url_env": "GENERAL_BASE_URL",
      "profile": {"capabilities": ["contract"], "context_window_tokens": 8192}
    },
    "long": {
      "model_env": "EXTRACT_MODEL", "api_key_env": "EXTRACT_API_KEY",
      "base_url_env": "EXTRACT_BASE_URL",
      "profile": {"capabilities": ["contract"], "context_window_tokens": 65536}
    }
  },
  "default_alias": "standard",
  "task_routes": {"document_extraction": "standard"},
  "fallback_routes": {"standard": ["long"]},
  "execution": {"max_attempts": 2, "timeout_seconds": 60, "max_concurrency": 4}
}
```

`ChatRequest` 和 `ModelRequest` 接受 `required_capabilities=("contract",)`。路由先检查所需后端方法和全部标签，再检查输入 Token 数加 `max_tokens` 的预留输出空间。调用方可以传入用部署模型分词器计算的完整 `input_tokens`。省略时按实际发送的消息、系统提示、历史和结构化 Schema 的 UTF-8 字节数及消息开销估算。未声明窗口的模型没有配置容量上限。

显式别名必须通过上述校验。自动选模先检查任务或默认别名及其配置的备用列表，再按注册顺序查找其他兼容模型，其中已声明窗口的模型优先。发生运行故障时，客户端只切换到路线中配置的备用模型。

## 失败回退和请求上限

`execution.max_attempts` 为单次逻辑调用共享的上限，默认 1。连接错误、超时和限流等瞬时失败优先切换可用备用模型，备用列表耗尽后可在剩余次数内重试当前模型。原生结构化输出错误可切换备用模型。每次重试、切换和退避共用总期限。已向调用方交付文本的流遇到错误会终止，已交付内容可以由业务方标记为中断。

内置工厂关闭 SDK 重试和重定向，结构化调用使用一次原生请求。端点的 `structured_tool_choice` 默认为 `forced`，也可配置 `auto` 或 `none` 以匹配供应商协议。自定义后端需遵守每次尝试最多发送一次供应商请求的约定。LangExtract 的每个分段和最终归并分别占用一次逻辑调用。

## 运行日志

在应用启动时配置输出。下面的设置将 JSONL 写入 `model-calls.jsonl`，单文件约 10 MB 时轮转，最多保留 5 个备份。容器部署可使用 `configure_model_logging(destination="stdout")`，保留期限由采集端设置。应用也可使用 Python logger `agent_components.model_calls` 接收 INFO 级别事件。

```python
from agent_components.models import configure_model_logging, model_log_context

configure_model_logging(
    destination="jsonl",
    path="model-calls.jsonl",
    max_bytes=10_000_000,
    backup_count=5,
    include_bodies=False,
)


async def run_job(client, request):
    with model_log_context(run_id="review-31", job_id="job-42", document_version="r3"):
        return await client.generate(request)
```

按 `request_id` 查业务请求，按 `call_id` 查一次逻辑调用，按 `attempt_id` 查某一模型的单次尝试。查看 `model.route` 可了解选模原因，`model.retry` 和 `model.fallback` 记录失败后的决策，`model.http.request` 记录已装配传输层的发送。聊天、结构化输出、LangExtract、Embedding 和重排序共享日志格式。用量保存供应商实际报告的数值，缺失项为 `null`。正文默认关闭，显式设置 `include_bodies=True` 后记录经过凭据脱敏的正文。
