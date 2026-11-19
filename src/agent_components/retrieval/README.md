# 检索组件

`RetrievalPipeline.search` 接收查询和本次授权可见的分块。使用托管知识库时，`ManagedKnowledgeBase.search` 从当前发布版本构建可见集合，并在返回前复核权限和版本。默认 LanceDB 适配器同时提供向量和中文全文检索，安装 `retrieval` 依赖组即可在应用进程内使用。[入库与索引初始化](../knowledge_base/README.md#创建知识库并首次发布)

## 查询模式

| `RetrievalOptions.mode` | 使用的能力 |
| --- | --- |
| `vector`（默认） | 注入的 Embedding Provider 和向量索引。入库与查询使用匹配的模型、维度和预处理 |
| `keyword` | LanceDB ICU 中文分词与 BM25。选择 `exact` 或 `dedup` 时只执行全文召回 |
| `hybrid` | 向量和 BM25 分别召回，再用 LanceDB RRF 按两路名次融合 |

## 在知识库中执行关键词检索

以下代码查询首次发布示例里的合同。用相同的 `kb` 实例与当前授权范围调用。关键词检索走已入库的叶子文本，`selection="exact"` 无需调用查询 Embedding。

```python
from agent_components.retrieval import RetrievalOptions

result = kb.search(
    "尾款 验收",
    authorized_document_ids=lambda: [document_id],
    document_ids=[document_id],
    options=RetrievalOptions(
        mode="keyword",
        recall_limit=20,
        top_k=4,
        selection="exact",
        parent_mode="replace",
        max_characters=4000,
    ),
)
for hit in result.hits:
    print(hit.match.document_id, hit.match.revision_id, hit.scores.bm25_score)
    print(hit.source_refs)

for window in result.context.windows:
    print(window.index_text, window.source_refs)
```

线上应通过权限服务生成 `authorized_document_ids`。独立使用 `RetrievalPipeline(index).search(..., visible_chunks=chunks)` 时，由应用提供已按文档、版本和访问范围过滤的集合，父块只有全文可见时才应纳入。

## 独立调用混合检索

完成知识库首次发布后，可在仓库根目录把下面的代码作为独立脚本运行。沿用入库的 Embedding 环境变量。需要重排时再提供完整的 `RERANK_ENDPOINT_URL`、模型标识 `RERANK_MODEL`，以及端点要求的 `RERANK_API_KEY`。

```python
import os
from contextlib import closing
from pathlib import Path

from agent_components.knowledge_base import KnowledgeBase
from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository
from agent_components.retrieval import (
    HTTPReranker,
    LanceDBIndex,
    RetrievalOptions,
    RetrievalPipeline,
)

from examples.providers import compatible_embeddings

root = Path("data/knowledge")
content = SQLiteKnowledgeRepository(root / "content.db")

# 演示环境只授权此合同。业务接入时从实时权限系统取得这些文档 ID。
visible = KnowledgeBase("contracts", content).published_chunks(document_ids=["DEMO-2026-001"])
endpoint = os.getenv("RERANK_ENDPOINT_URL")
reranker = None
if endpoint:
    reranker = HTTPReranker(
        endpoint_url=endpoint,
        model=os.environ["RERANK_MODEL"],
        api_key=os.getenv("RERANK_API_KEY"),
        timeout=5,
    )

with closing(compatible_embeddings()) as embedding:
    index = LanceDBIndex(
        root / "vectors", dimensions=embedding.dimension, table_name="contracts_v1"
    )
    pipeline = RetrievalPipeline(index, embedding, reranker=reranker)
    result = pipeline.search(
        "采购合同的尾款支付条件是什么？",
        visible_chunks=visible,
        options=RetrievalOptions(
            mode="hybrid",
            rerank=reranker is not None,
            reranker_failure="fallback",
            selection="exact",
            parent_mode="replace",
        ),
    )

for hit in result.hits:
    print(hit.match.id, hit.match.revision_id, hit.scores, hit.source_refs)
for warning in result.warnings:
    print(warning.code, warning.message)
```

此示例在应用层组装查询范围。调用方可直接使用 `ManagedKnowledgeBase.search`，在同一次请求内复核授权文档及发布快照。

## 候选处理与结果

| 参数 | 行为 |
| --- | --- |
| `recall_limit`、`top_k` | 前者限制每路叶子候选，后者限制最终命中数 |
| `rerank=True` | 注入 `RerankerProvider` 后启用；默认失败抛异常，`reranker_failure="fallback"` 时保留上一步排序并返回提示 |
| `selection="exact"`、`"dedup"`、`"mmr"` | 精确去重、含近重复去重、基于候选向量的多样性选择 |
| `parent_mode`、`before`、`after` | 父块补充或替换，以及同版本的相邻块扩展 |
| `max_characters` | 扩展上下文的总字符预算，超预算块记录在 `context.omitted_ids` |

`hits` 保留每个叶子命中的原始索引分值、各通道名次、RRF 和重排分数及来源。未执行的阶段使用 `None`。LanceDB 向量距离越小越近，BM25 和 RRF 分数越大越相关。`context.windows` 返回去掉重叠正文后的上下文与来源。使用 `text` 和 `source_refs` 做原文引用，使用 `index_text` 提交带表头前缀的上下文。

可用 `HTTPReranker` 连接 Cohere 兼容端点，也可自行实现 Provider。默认 LanceDB 在首次关键词查询时建立 ICU 全文索引，也可提前调用 `build_fts`。业务术语和编号的召回效果需结合目标语料验收。[HTTP 重排接入与失败示例](../../../docs/examples/retrieval.md#重排序与分数) · [检索验收](../../../docs/reviews/KNOWLEDGE_RETRIEVAL_REVIEW.md)
