# 模型调用组件

应用可注入已创建的模型，或用模型包的 OpenAI 兼容工厂装配 AgentScope 客户端。结构化抽取通过 `ModelClient` 调用，Embedding 与重排可分别注入分块和检索。

| 入口 | 职责 |
| --- | --- |
| [client.py](client.py) | 统一调用入口，组合选模、执行控制和请求级事件 |
| [routing.py](routing.py) | 显式模型别名、任务映射、默认模型与别名校验 |
| [call_logging.py](call_logging.py) | 通过 `agent_components.model_calls` 输出 JSON 事件，由应用设置日志落点和保留周期 |
| [providers/agentscope.py](providers/agentscope.py) | 转换 AgentScope 原生结构化请求、响应与 Token 用量 |
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

## 调用日志与兼容入口

日志提供逻辑调用的开始、完成和失败事件，记录请求 ID、调用 ID、任务、模型别名、耗时和已知用量。启用 `MODEL_CALL_LOGGER_NAME` 对应的 Python logger 接收事件。AgentScope 原生结构化协议可在同一次逻辑调用中尝试多种策略，实际 HTTP 次数由供应商请求遥测核对。[重试与调用次数](../../../docs/examples/models.md#日志重试与调用次数)

原有的 `agent_components.models.ModelClient`、`agent_components.adapters.agentscope.model`、`agent_components.adapters.langextract` 和 `agent_components.retrieval.reranking` 仍可导入。现有分块与检索可以继续使用自己的 Provider 协议。
