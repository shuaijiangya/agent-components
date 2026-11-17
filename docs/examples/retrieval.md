# 检索原语、上下文扩展与选择

源码：[retrieval.py](../../examples/retrieval.py)。这些组件接收已有块和向量，当前没有统一封装 embedding→BM25→融合→重排序→生成的 RAG 流程。

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
- `index --build-fts` 仅建立后端全文索引；当前未提供中文分词、BM25 查询或混合融合接口。

## 父块与相邻块扩展

输入为**已通过权限和版本过滤**的 `ChunkResult` JSON；`hits.json` 是实际命中 ID 按相关性排序的数组。命中必须存在于当前 corpus，父块也必须可见。

```sh
uv run --no-sync python -m examples.retrieval context data/parent-child.json --hits data/hits.json --before 1 --after 1 --parent-mode replace --max-characters 8000 --output data/context.json
```

`parent-mode` 为 `none/append/replace`；默认不跨父章节补邻块。扩展会合并重叠区间、检查重叠文本一致性、在不连续序号处停止，不能跨越被权限过滤掉的缺口。输出 `windows/selected_ids/omitted_ids`，预算不足的项会被记录。

`ContextWindow.index_text` 也属于计算属性；序列化后要恢复模型或拼接 `context_prefix + text`，避免丢失重复表头。

直接接入公共 API：

```python
from agent_components.context import ContextExpander

context = ContextExpander().expand(
    hits, visible_corpus,
    before=1, after=1, parent_mode="replace", max_characters=8000,
)
```

组件不从数据库自动加载父块，不识别租户，不为使用方决定授权范围。

## MMR 与去重

输入为 `RetrievalCandidate` 数组：`chunk/relevance/vector`。MMR 要求查询和候选具有同源、非零、有限且维度一致的向量；去重可以不带向量。

```sh
uv run --no-sync python -m examples.retrieval select examples/data/candidates-fixture.json --method mmr --query examples/data/query-fixture.json --limit 1 --relevance-weight 0.5 --output data/mmr.json
uv run --no-sync python -m examples.retrieval select examples/data/candidates-fixture.json --method dedup --threshold 0.85 --output data/unique.json
```

MMR 使用查询余弦相关性与已选候选最大余弦冗余进行贪心选择，不直接混用未归一化的 BM25 分数。去重使用规范化文本的精确比较／字符 shingle Jaccard，并返回 `dropped` 的保留对象和相似度依据；它不能判断语义事实是否相同。

当前候选池默认最多 2000，避免不受控的比较成本。MMR、去重与上下文扩展的先后顺序由使用方按业务验证，不在组件内固定。
