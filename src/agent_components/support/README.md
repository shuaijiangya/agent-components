# 独立结构化数据与证据抽取

`agent_components.support.Extractor` 接收一个完整 `Document`、一个 `Chunk` 或一组 `Chunk`，以及 JSON Schema 字典或 Pydantic 模型。调用方提供结构化模型端口即可接入，业务流程、数据保存和结果消费由调用方编排。

| 策略 | 执行方式 | 模型任务路由 |
| --- | --- | --- |
| `structured` | 一次逻辑模型调用，按 Schema 和 Pydantic 校验返回数据 | `structured_extraction` |
| `evidence` | LangExtract 按输入分段和对齐引文，再经一次归并生成业务结构及字段证据 | `document_extraction` |

## 接入

项目使用 Python 3.12。运行以下示例需要 AgentScope 和 LangExtract。用自己的结构化模型实现时，普通抽取只需要基础安装，证据模式额外安装 `extraction`。

```bash
uv sync --locked --extra agent --extra extraction
```

在仓库根目录运行。模型配置从环境变量读取，变量名和示例文件见 [模型包接入说明](../models/README.md)。也可以向 `Extractor` 传入实现 `async generate(ModelRequest) -> ModelOutput` 的模型对象。

```python
import asyncio
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent_components.core.contracts import Document
from agent_components.models.providers.openai_compatible import (
    OpenAIModelSettings,
    openai_compatible_clients,
)
from agent_components.support import (
    EvidenceOptions,
    ExampleCitation,
    ExtractionExample,
    ExtractionOptions,
    Extractor,
)


class Payment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: int = Field(ge=0)
    currency: str


class ContractFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payment: Payment


async def main() -> None:
    settings = OpenAIModelSettings.model_validate_json(
        Path("examples/data/models.json").read_bytes()
    )
    document = Document(
        id="contract-42",
        revision_id="r2",
        text="第五条 合同总价为人民币3200元，验收后一次性支付。",
    )
    example = ExtractionExample(
        text="本合同总价人民币1200元。",
        data={"payment": {"amount": 1200, "currency": "CNY"}},
        evidence=(
            ExampleCitation(field_path="/payment/amount", quote="总价人民币1200元"),
            ExampleCitation(field_path="/payment/currency", quote="人民币"),
        ),
    )
    async with openai_compatible_clients(settings) as (client, _):
        extractor = Extractor(client)
        plain = await extractor.extract(document, ContractFields)
        cited = await extractor.extract(
            document,
            ContractFields,
            ExtractionOptions(
                strategy="evidence",
                instructions="只提取合同总价，金额单位为元，币种使用 ISO 4217。",
                request_id="contract-42-r2-extraction",
                examples=(example,),
                timeout_seconds=90,
                max_output_tokens=2000,
                evidence=EvidenceOptions(
                    max_char_buffer=2000,
                    extraction_passes=1,
                    max_model_calls=32,
                ),
            ),
        )
        print(plain.model_dump_json(indent=2))
        print(cited.model_dump_json(indent=2))


asyncio.run(main())
```

使用动态 Schema 时，第二个参数可传 `ContractFields.model_json_schema()`，也可传业务方维护的 JSON Schema 字典。引用从随请求提供的 Schema 解析。普通 JSON 样例放在 `ExtractionExample.data`。使用知识库召回结果时，直接传入已有的 `Chunk` 对象，证据以 `Chunk.text` 为偏移基准，表格重复表头 `context_prefix` 不计入原文。

## 读取结果

| 字段 | 使用方式 |
| --- | --- |
| `status` | `success` 表示业务数据通过校验且当前策略没有报告缺口。证据策略返回 `partial` 时，`data` 仍通过 Schema 校验，使用前应检查 `errors` 和 `warnings`。 `failed` 表示没有可交付结果。 |
| `data` / `candidates` | `data` 为目标业务值，字段可包含对象、数组、标量和 Schema 允许的 `null`。校验失败或全部引文均未定位的结果放入 `candidates`。根值可为 `null` 时结合 `status` 判断。 |
| `evidence` | `field_path` 是最终 JSON 值的 RFC 6901 路径，例如 `/payment/amount` 或 `/items/0/amount`。同一字段可关联多条原文。 |
| `errors` / `warnings` | 包含机器可用的 `code`、相关输入 ID、字段路径或从 1 开始的分段请求序号。 |
| `meta` / `usage` | 包含请求 ID、模型、逻辑调用数、耗时和每个输入的文档版本、范围、分段结果。用量字段只有在每次逻辑调用都报告该指标时才给出总计。 |

使用 `ModelClient` 时，模型调用日志沿用 `ExtractionOptions.request_id`。省略时优先沿用外层 `model_log_context` 的请求 ID，否则由抽取器生成。原生 LangExtract 的每次分段调用记录该输入的 `document_id` 与 `revision_id`，同时将版本写入 `document_version`。普通抽取与证据归并仅记录全部输入共有的文档身份，同一文档的多个版本可以通过 `meta.coverage` 逐项查看。详见[模型调用日志](../models/README.md)。

证据的 `start:end` 是传入 `Document.text` 或 `Chunk.text` 的 Python 字符区间。对已定位的证据，`input.text[start:end] == quote`。精确匹配为 `exact`，近似匹配为 `fuzzy` 并保留 `proposed_quote`，无法定位为 `unlocated` 且偏移为空。首条 `source_refs` 给出规范文档中的全局偏移和版本。原文已有来源映射时，交集范围可带元素 ID、页码及元素级矩形。

## 执行限制

- `evidence.max_char_buffer` 控制 LangExtract 原生分段。可设置 `context_window_chars` 补充上一段上下文，`extraction_passes` 增加分段处理轮次。范围以传入的文档或块为准，`coverage` 的分段计数包含各轮和预算跳过的分段。
- `max_model_calls` 统计逻辑调用，包含一次归并调用。同一逻辑调用内的重试与模型回退共用统一模型入口的尝试次数上限，可通过 `call_id` 查看各次尝试。超过分段预算时，已获得的引文仍可用于归并，结果会报告缺失范围。
- `max_synthesis_chars` 限制归并输入的字符数。超出时返回 `synthesis_budget_exceeded`。`timeout_seconds` 到期时返回 `extraction_timeout` 并停止发起后续模型请求。调用方的取消会传播。
- 模型归并负责业务含义、记录分组和字段值规范化。代码校验 Schema、字段引用和原文定位。字段是否受到引文充分支持，由业务数据验收判断。
- `Extractor(..., evidence_backend=...)` 可注入实现 `EvidenceBackend` 的自定义组件。旧路径 `agent_components.extraction.Extractor` 保留原来的显式证据后端配置。
