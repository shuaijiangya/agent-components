# 检索接入、重排与上下文扩展

文本检索入口和存储端口位于 `agent_components.retrieval`。`RetrievalPipeline` 可独立使用，或通过 `ManagedKnowledgeBase.search` 读取发布范围并在返回前复核实时授权。使用仓库中的合同运行查询，可直接从包 README 开始。[检索包 README](../../src/agent_components/retrieval/README.md) · [存储端口](../../src/agent_components/retrieval/vector_store.py) · [知识库接入](knowledge-base.md)

## 从查询文本检索上下文

`RetrievalPipeline.search()` 接受查询字符串和当前请求可见的 `Chunk` 集合。它先按允许的叶子 ID 召回，再融合、重排、选择候选并扩展上下文。[链路源码](../../src/agent_components/retrieval/pipeline.py)

先准备同一批已入库的叶子块及其向量。`IndexRecord.id`、`document_id`、`revision_id` 与 `Chunk` 对应，索引文本使用 `chunk.index_text`。查询与入库共享 Embedding 模型、版本、维度和预处理。下面的例子使用现有 OpenAI 兼容工厂，请先设置 `EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`EMBEDDING_DIMENSION` 和 `EMBEDDING_API_KEY`，并安装 `retrieval` 和 `agent` 依赖组。[工厂实现](../../examples/providers.py)

```python
from contextlib import closing
from pathlib import Path

from agent_components.core.contracts import ChunkResult
from agent_components.retrieval import LanceDBIndex, RetrievalOptions, RetrievalPipeline
from examples.providers import compatible_embeddings

# 文件应只包含本次请求已授权、已发布的块。父块应在全文可见时才加入。
visible = ChunkResult.model_validate_json(Path("data/visible-chunks.json").read_bytes()).chunks

with closing(compatible_embeddings()) as embedding:
    index = LanceDBIndex(Path("data/lancedb"), dimensions=embedding.dimension)
    pipeline = RetrievalPipeline(index, embedding)
    result = pipeline.search(
        "设备验收不通过时，尾款应在什么时候支付？",
        visible_chunks=visible,
        options=RetrievalOptions(
            mode="hybrid",
            selection="exact",
            recall_limit=30,
            top_k=5,
            parent_mode="none",
            before=1,
            after=1,
            max_characters=6000,
        ),
    )

for window in result.context.windows:
    print(window.document_id, window.revision_id, window.start, window.end)
    print(window.index_text)
```

- `mode="vector"` 调用 Embedding 召回；`mode="keyword"` 以 LanceDB ICU 中文分词执行 BM25；`mode="hybrid"` 用 LanceDB RRF 融合两个列表。仅关键词召回且采用去重时，`RetrievalPipeline(index)` 便可运行。
- `recall_limit` 是每个通道的候选数上限。`top_k` 是选择后的叶子数，不计补充的父块或邻块。`rrf_k` 默认为 60，融合分数按通道的名次计算。
- `selection="exact"` 去除同一文档版本的相同区间及规范化后文本相同的候选。`selection="dedup"` 是默认值，并额外使用字符片段 Jaccard 判断近重复。`selection="mmr"` 使用已有候选向量衡量冗余。
- `parent_mode="none"` 使用邻块配置。`append` 额外纳入父块，`replace` 以父块替换有父关系的命中。后两种要求相关父块在可见集合中。
- 可见叶子 ID 在 Top K 前过滤。空作用域直接返回空结果。索引行的版本、文档或文本与可见块不一致时，调用会报错。

`visible_chunks` 是当前请求的授权语料快照。此入口在内存中持有该快照，并将可见叶子 ID 传给 LanceDB 过滤。查询为同步调用。业务服务可在自己的线程池调度，并管理 Provider 超时与生命周期。

## 重排序与分数

`HTTPReranker` 接受完整端点 `endpoint_url`、服务端模型 ID、可选令牌环境变量和以秒为单位的超时。适配 Cohere 兼容的 `model/query/documents` 请求及 `results[index,relevance_score]` 响应。为已授权的候选提交检索文本，使用有权处理这些文档的端点。[Provider 源码](../../src/agent_components/models/providers/reranking.py)

```python
from agent_components.retrieval import HTTPReranker, RetrievalOptions, RetrievalPipeline

provider = HTTPReranker(
    endpoint_url="https://rerank.example.com/v1/rerank",
    model="your-rerank-model",
    api_key_env="RERANK_API_KEY",
    timeout=5,
)
pipeline = RetrievalPipeline(index, embedding, reranker=provider)
result = pipeline.search(
    "设备验收条件",
    visible_chunks=visible,
    options=RetrievalOptions(mode="hybrid", rerank=True, reranker_failure="fallback"),
)
for hit in result.hits:
    print(hit.match.id, hit.match.revision_id, hit.scores, hit.source_refs)
```

- `reranker_failure="raise"` 为默认策略，超时、传输失败、HTTP 错误和无效结果抛 `RerankerError`。选 `"fallback"` 时沿用前一阶段的顺序，结果附带 `reranker_fallback` 提示。
- `result.hits` 记录向量原始分数及类型、向量和 BM25 名次、BM25、RRF、重排分数与来源。缺失阶段使用 `None`。距离越小越近；BM25 和 RRF 越大越相关。`matches` 保留索引原始分值，适合兼容已有代码。
- `result.dropped` 记录被去重的块、保留块和原因。`result.context` 返回上下文窗口、来源、纳入块 ID 与超过预算的块 ID。上下文合并重叠正文和同来源锚点的重复区间，保留不同页、元素及定位精度。

## 嵌入式 LanceDB

向量由使用方提供。输入为 `IndexRecord` JSONL：`id/document_id/revision_id/text/vector`；数据库运行在当前进程，不另起服务。

```sh
uv run --no-sync python -m examples.retrieval index --db data/lancedb --dimensions 3 --records examples/data/index-fixture.jsonl
uv run --no-sync python -m examples.retrieval search --db data/lancedb --dimensions 3 --query examples/data/query-fixture.json --limit 5 --output data/matches.json
```

这里三维向量是**固定接口验收数据**，只检查写入与查询链路。线上把它们替换成真实 Embedding，查询与文档必须使用同一模型、版本、维度和预处理。

- 写入按 ID upsert，可重复执行；单批内重复 ID 会失败。
- `--batch-size` 默认 128。多批写入没有整文件事务；后续批失败时，之前已提交的批不会回滚。跨批同 ID 以最后提交的数据为准。生产需使用方管理临时索引、版本发布与失败恢复。
- `--dimensions` 必须与既有表和所有向量匹配，升级模型应使用新表／新目录。
- 返回 `score_kind="distance"`，越小越近；不能作为越大越好的 BM25 分值直接相加。
- `--document-ids path.json` 读取允许的文档 ID 数组；空数组返回空结果。过滤只是查询条件，不能代替权威权限与版本检查。
- `index --build-fts` 提前建立 ICU 全文索引。首次关键词查询也可按需建立。中文领域词和业务编号的排序阈值用实际问答样本验收。

## 父块与相邻块扩展

输入为**已通过权限和版本过滤**的 `ChunkResult` JSON；`hits.json` 是实际命中 ID 按相关性排序的数组。命中必须存在于当前 corpus，父块也必须可见。

```sh
uv run --no-sync python -m examples.retrieval context data/parent-child.json --hits data/hits.json --before 1 --after 1 --parent-mode replace --max-characters 8000 --output data/context.json
```

`parent-mode` 为 `none/append/replace`；默认不跨父章节补邻块。扩展会合并重叠区间、检查重叠文本一致性、在不连续序号处停止，不能跨越被权限过滤掉的缺口。输出 `windows/selected_ids/omitted_ids`，预算不足的项会被记录。

`ContextWindow.index_text` 也属于计算属性；序列化后要恢复模型或拼接 `context_prefix + text`，避免丢失重复表头。

直接接入公共 API：

```python
from agent_components.retrieval import ContextExpander

context = ContextExpander().expand(
    hits,
    visible_corpus,
    before=1,
    after=1,
    parent_mode="replace",
    max_characters=8000,
)
```

组件不从数据库自动加载父块，不识别租户，不为使用方决定授权范围。

## MMR 与去重

输入为 `RetrievalCandidate` 数组：`chunk/relevance/vector`。MMR 要求查询和候选具有同源、非零、有限且维度一致的向量；去重可以不带向量。

```sh
uv run --no-sync python -m examples.retrieval select examples/data/candidates-fixture.json --method mmr --query examples/data/query-fixture.json --limit 1 --relevance-weight 0.5 --output data/mmr.json
uv run --no-sync python -m examples.retrieval select examples/data/candidates-fixture.json --method dedup --threshold 0.85 --output data/unique.json
```

纯向量模式的 MMR 使用查询余弦相关性。关键词、融合或已重排模式按上游顺序缩放相关度，再与已选候选的最大余弦相似度做权衡。精确去重与近重复比较各自返回保留对象和相似度依据。

独立候选选择原语默认上限 2000。检索入口按 `recall_limit` 为每个通道限制工作量，默认每通道 30。单独使用原语时，调用方可以按业务调整组合顺序。
