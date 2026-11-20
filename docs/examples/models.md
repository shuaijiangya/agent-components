# 模型路由、结构化提取与 LangExtract

示例命令通过模型包装配 AgentScope 2.0.11 原生模型，并请求应用指定的 OpenAI 兼容端点。[模型包 README](../../src/agent_components/models/README.md) · [CLI 源码](../../examples/models.py)

使用硅基流动时，可按供应商示例配置两个聊天模型，并调用 Embedding 和重排。[硅基流动多模型示例](siliconflow-models.md)

## 显式配置端点与策略

[`models.json`](../../examples/data/models.json) 只保存环境变量名称：

```json
{
  "default_alias": "general",
  "task_routes": {"structured_extraction": "extract", "document_extraction": "extract"},
  "models": {
    "general": {"model_env": "GENERAL_MODEL", "api_key_env": "GENERAL_API_KEY", "base_url_env": "GENERAL_BASE_URL"},
    "extract": {"model_env": "EXTRACT_MODEL", "api_key_env": "EXTRACT_API_KEY", "base_url_env": "EXTRACT_BASE_URL"}
  },
  "execution": {"timeout_seconds": 120, "max_concurrency": 4, "max_attempts": 1}
}
```

由运行环境注入六个变量。模型名、base URL 都必须显式配置，例如内部服务的 `http://model-service:8000/v1`；密钥通过 Secret 管理。示例不会替你选择免费端点或默认公网模型。即使两个别名指向同一个服务，也要提供对应环境变量。

路由顺序：**请求 `model_alias` > `task_routes[task]` > `default_alias`**。缺失的环境变量、未知路由目标在建立请求前失败。原生客户端在成功、异常及取消时关闭。

SDK 中的 `openai_compatible_clients` 使用 `OpenAICredential` 装配 `OpenAIChatModel`。其他供应商可实现公共 `StructuredModel.generate(ModelRequest) -> ModelOutput` 后注册到 `ModelClient`。[OpenAI 兼容装配源码](../../src/agent_components/models/providers/openai_compatible.py)

## 模型级结构化输出

```sh
uv run --no-sync python -m examples.models generate examples/data/contract.txt --config examples/data/models.json --schema examples/data/contract.schema.json --task structured_extraction --request-id contract-review-001 --output data/model-output.json
```

返回 `ModelOutput(data,usage,model)`。当前 AgentScope 适配器要求 object 根 JSON Schema，通过原生工具结构化输出调用。目标服务必须支持相应 tools/tool_choice 协议。

`ModelClient` 提供路由、资源控制和事件日志，不代表业务字段准确率保证。模型返回的字面金额、日期和主体仍要根据业务规则验收。

## 文档／块＋Schema → 结构化提取

```sh
uv run --no-sync python -m examples.models extract examples/data/contract.txt --config examples/data/models.json --schema examples/data/contract.schema.json --instructions '仅提取原文明确给出的数值，区分工作日与自然日。' --request-id extract-001 --output data/extracted.json
```

输入形态包括：

| `--input-kind` | 文件内容 |
| --- | --- |
| `text`（默认） | UTF-8 原文 |
| `document` | `Document` JSON |
| `parsed` | `ParsedDocument` JSON |
| `chunk` | 一个 `Chunk` JSON |
| `chunks` | `ChunkResult` JSON；仅使用 leaves |

例如，已有块直接抽取：

```sh
uv run --no-sync python -m examples.models extract data/chunks.json --input-kind chunks --config examples/data/models.json --schema examples/data/contract.schema.json --model-alias general --output data/extracted.json
```

返回 `ExtractionResult`，包含 `status/data/candidates/errors/input_ids/usage`。普通提取不保证字段证据。若后端已返回数据但 SDK 校验失败，`status=failed`、`data=null`，原始数据进入 `candidates`，退出码 1。

**原生模型先拒绝输出时有所不同**：AgentScope 会先校验 JSON Schema，不合法则抛 `StructuredOutputError`；结果尚未交给 `Extractor`，所以没有可返回的 `candidates`，命令退出码为 2。调用方必须同时处理结果失败和后端异常。

直接接入可以传 Pydantic 类，此时 SDK 还会执行该类的严格校验和自定义 validator：

```python
from pydantic import BaseModel, Field
from agent_components.extraction import Extractor
from agent_components.core.contracts import ExtractionOptions

class ContractFields(BaseModel):
    total_amount_cny: int = Field(ge=0)
    final_payment_days: int = Field(ge=0)

result = await Extractor(model_client).extract(
    content, ContractFields,
    ExtractionOptions(model_alias="extract", request_id="extract-001"),
)
```

Pydantic 定义数据与校验，不负责理解原文；字段提取由模型完成。该组件不进行检索、不执行操作、不自动截断长文档；调用方要控制上下文长度、重复事实和多块冲突。

## 日志、重试与调用次数

`agent_components.model_calls` 在 stderr 输出 `model.started/completed/failed` JSON，包含请求标识、任务、别名、耗时、用量和异常类型；SDK 不写正文或供应商异常消息。不要把正文或密钥放到 request_id、task、别名或模型名称中。

示例同时关闭 AgentScope 瞬时错误重试和 OpenAI HTTP 客户端重试，但 **AgentScope 结构化输出兼容 fallback 仍存在**：forced、auto、none 等策略在结构不合法或协议不兼容时可能切换。当前锁定的 OpenAI 适配器在测试的结构非法场景会发送 3 次 HTTP 请求。`max_attempts=1` 不能当作“一次 HTTP 请求”的保证。

SDK 日志记录一次逻辑调用，最终成功响应的 usage 不包含所有失败 fallback 的消耗。线上计费和供应商请求级审计应以服务端／供应商遥测补齐；本示例不声称能完整统计账单。

## LangExtract Provider 桥接

```sh
uv run --no-sync python -m examples.models langextract examples/data/langextract-prompts.json --config examples/data/models.json --schema examples/data/langextract-envelope.schema.json --output data/langextract-provider.json
```

输入是 LangExtract 提示字符串数组，Schema 为 extraction envelope；输出是每条提示的 `ScoredOutput.output` 解码值数组。`document_extraction` 使用同一任务路由。运行时通过 `asyncio.to_thread` 执行同步 `infer`，原事件循环继续处理异步模型请求。

```python
provider = create_provider(
    model_client, loop=asyncio.get_running_loop(), output_schema=envelope_schema,
)
outputs = await asyncio.to_thread(lambda: list(provider.infer(prompts)))
```

这是 Provider 接口验收，不是完整长文档证据组件。任意业务 Schema 到 extraction classes 的映射、少样本示例、长文档分段、多轮提取、原文证据对齐和结果合并仍需独立实现 `EvidenceBackend`。当前 `Extractor(strategy="evidence")` 未注入后端会明确报错，不能用这个桥接示例代替证据能力验收。
