# 硅基流动多模型接入示例

使用平台的 OpenAI 兼容端点 `https://api.siliconflow.cn/v1`。以下四个模型 ID 均能在该端点的模型目录查到。示例使用虚构合同和请假制度，便于核验用途和返回结构。[文本调用文档](https://docs.siliconflow.cn/docs/userguide/capabilities/text-generation) · [重排接口](https://docs.siliconflow.cn/docs/api/rerank-post)

| 模型 | 本项目用途 |
| --- | --- |
| `deepseek-ai/DeepSeek-V3.2` | 按任务路由执行结构化提取和 LangExtract Provider 请求 |
| `Qwen/Qwen3-30B-A3B-Instruct-2507` | 默认模型，可用显式别名覆盖任务路由 |
| `Qwen/Qwen3-Embedding-0.6B` | 对入库分块和查询计算向量 |
| `Pro/BAAI/bge-reranker-v2-m3` | 对混合召回候选做重排 |

## 配置端点和密钥

在仓库根目录运行。下面是 Bash 示例，输入密钥时终端隐藏回显。服务部署时使用环境变量或密钥管理器注入 `SILICONFLOW_API_KEY`。

```bash
uv sync --locked --extra agent --extra extraction --extra retrieval
read -rsp 'SiliconFlow API key: ' SILICONFLOW_API_KEY
printf '\n'
export SILICONFLOW_API_KEY

export SILICONFLOW_BASE_URL='https://api.siliconflow.cn/v1'
export GENERAL_BASE_URL="$SILICONFLOW_BASE_URL"
export EXTRACT_BASE_URL="$SILICONFLOW_BASE_URL"
export GENERAL_API_KEY="$SILICONFLOW_API_KEY"
export EXTRACT_API_KEY="$SILICONFLOW_API_KEY"
export GENERAL_MODEL='Qwen/Qwen3-30B-A3B-Instruct-2507'
export EXTRACT_MODEL='deepseek-ai/DeepSeek-V3.2'
```

## 四类调用的流式和非流式示例

以下命令共用上面的环境变量。每个调用方式对应独立函数，可直接查看和复用。[八种调用方式的 Python 源码](../../examples/siliconflow_modes.py)

```bash
uv run --no-sync python -m examples.siliconflow_modes chat
uv run --no-sync python -m examples.siliconflow_modes chat --stream

uv run --no-sync python -m examples.siliconflow_modes extract
uv run --no-sync python -m examples.siliconflow_modes extract --stream

uv run --no-sync python -m examples.siliconflow_modes embed
uv run --no-sync python -m examples.siliconflow_modes embed --stream

uv run --no-sync python -m examples.siliconflow_modes rerank
uv run --no-sync python -m examples.siliconflow_modes rerank --stream
```

| 能力 | 一次返回 | 逐步返回 |
| --- | --- | --- |
| 普通聊天 | AgentScope 原生客户端返回完整文本 | 原生客户端的 OpenAI HTTP 连接接收 SSE，逐条输出 `text_delta`；正常结束后输出 `completed` |
| 结构化抽取 | `Extractor.extract` 调用 `ModelClient`，按任务选模并校验 Pydantic 业务结构 | 通过 Chat Completions 的 JSON 模式接收 SSE，`json_delta` 供预览；正常结束且通过 Pydantic 校验后输出 `validated` |
| Embedding | 一个请求返回一批完整向量 | 生成器按批发起请求，逐批返回完整向量、维度与输入起始位置 |
| 重排 | 一个查询的全部候选作为一批请求，返回完整降序排名 | 多个查询由生成器逐个处理，每个查询返回一份完整排名 |

普通聊天和 SSE 示例默认使用 `general` 别名。非流式抽取按 `structured_extraction` 任务选择 `extract`；两类文本命令均可通过 `--model-alias` 显式选模。SSE 复用原生模型的连接。当前的 `ModelClient.generate` 提供非流式路由、执行控制和日志；流式示例中的终止检查由调用方处理。[硅基流动流式协议](https://docs.siliconflow.cn/docs/userguide/capabilities/stream-mode)

流式抽取的 JSON 片段可用于预览。收到 `validated` 后才将对象交给业务逻辑。遇到超时、截断、缺少终止原因或 Schema 校验失败，命令结束并返回失败码，不产生 `validated` 事件。Embedding 和重排的逐步返回由应用层生成器实现。重排每次提交一个查询的完整候选集，保留该候选集上的排序含义。[硅基流动重排接口](https://docs.siliconflow.cn/docs/api/rerank-post)

## 按任务选模型和显式切换

两条命令输入相同的示例文档与 Schema。第一条按 `structured_extraction` 任务选出 DeepSeek，第二条用 `--model-alias general` 选出 Qwen。用输出的 `model` 字段确认实际选中的模型。

```bash
uv run --no-sync python -m examples.models generate examples/data/contract.txt \
  --config examples/data/models.json --schema examples/data/contract.schema.json \
  --task structured_extraction --request-id sf-deepseek

uv run --no-sync python -m examples.models generate examples/data/contract.txt \
  --config examples/data/models.json --schema examples/data/contract.schema.json \
  --task structured_extraction --model-alias general --request-id sf-qwen
```

## 发送普通文本请求

模型工厂同时提供按别名索引的 AgentScope 原生客户端。这里用 `general` 别名调用 Qwen 文本模型。

```python
import asyncio
from pathlib import Path

from agentscope.message import UserMsg

from agent_components.models.providers.openai_compatible import (
    OpenAIModelSettings,
    openai_compatible_clients,
)


async def main() -> None:
    settings = OpenAIModelSettings.model_validate_json(
        Path("examples/data/models.json").read_bytes()
    )
    async with openai_compatible_clients(settings) as (_, native):
        response = await native["general"](
            [UserMsg(name="user", content="用一句话概括：尾款在验收后30日内支付。")],
            max_tokens=80,
        )
        print(response.content)


asyncio.run(main())
```

## 通过 LangExtract Provider 调用

LangExtract 桥接使用同一模型注册，按 `document_extraction` 任务调用 DeepSeek。可用 `--model-alias general` 显式切到 Qwen。输出为 LangExtract 请求和响应封套，业务字段证据对齐由应用提供的证据后端处理。

```bash
uv run --no-sync python -m examples.models langextract examples/data/langextract-prompts.json \
  --config examples/data/models.json --schema examples/data/langextract-envelope.schema.json
```

## 用 Embedding 和重排跑混合检索

将下方代码保存为 `siliconflow_retrieval_demo.py`，在以上变量已就绪的同一个终端运行 `uv run --no-sync python siliconflow_retrieval_demo.py`。示例写入临时 LanceDB，返回向量、BM25、RRF 和重排分数。索引使用 1024 维，入库与查询共用同一个 Embedding 实例。LanceDB 会检查服务实际返回的向量维度。[Qwen Embedding 模型说明](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)

```python
import os
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import Document
from agent_components.models.providers.openai_compatible import OpenAICompatibleEmbeddings
from agent_components.models.providers.reranking import HTTPReranker
from agent_components.retrieval import (
    IndexRecord,
    LanceDBIndex,
    RetrievalOptions,
    RetrievalPipeline,
)

base_url = os.environ["SILICONFLOW_BASE_URL"].rstrip("/")
embedding = OpenAICompatibleEmbeddings(
    model="Qwen/Qwen3-Embedding-0.6B",
    api_key=os.environ["SILICONFLOW_API_KEY"],
    base_url=base_url,
    dimension=1024,
)
reranker = HTTPReranker(
    endpoint_url=f"{base_url}/rerank",
    model="Pro/BAAI/bge-reranker-v2-m3",
    api_key_env="SILICONFLOW_API_KEY",
)

documents = (
    Document(id="contract", text="甲方在验收通过后30日内支付合同尾款。"),
    Document(id="leave-policy", text="员工请假应提前提交申请。"),
)
chunker = Chunker()
policy = ChunkPolicy(strategy="character", size=80)
leaves = tuple(leaf for doc in documents for leaf in chunker.chunk(doc, policy).leaves)

with closing(embedding), TemporaryDirectory() as directory:
    index = LanceDBIndex(Path(directory) / "index", dimensions=embedding.dimension)
    vectors = embedding.embed_batch([leaf.index_text for leaf in leaves])
    index.upsert(
        [
            IndexRecord(
                id=leaf.id,
                document_id=leaf.document_id,
                revision_id=leaf.revision_id,
                text=leaf.index_text,
                vector=tuple(vector),
            )
            for leaf, vector in zip(leaves, vectors, strict=True)
        ]
    )
    result = RetrievalPipeline(index, embedding, reranker=reranker).search(
        "合同尾款在什么时候支付？",
        visible_chunks=leaves,
        options=RetrievalOptions(
            mode="hybrid", rerank=True, recall_limit=2, top_k=2, selection="exact"
        ),
    )

for hit in result.hits:
    print(hit.match.document_id, hit.scores.model_dump(exclude_none=True))
```

应用服务器需要能访问模型端点。现有 `HTTPReranker` 通过直连 HTTPS 发送请求。向量空间由 Embedding 模型和实际维度共同确定。更换模型时建立新索引并重新计算向量。[知识库接入](../../src/agent_components/knowledge_base/README.md)
