# 轻量知识库与检索验收

按设计 §4 和 §9.1 验收知识库与检索 SDK。内容、文件与索引使用真实 SQLite、本地文件和 LanceDB。重排序通过真实本地 HTTP 请求验证协议。测试向量和重排分值由可控数据提供。[设计文档](../design-summary.md#4-轻量知识库与检索组件)

| 验收项 | 实现 | 验证 |
| --- | --- | --- |
| 目录创建、查询、更新、删除和隔离 | SQLite 目录 CRUD、独占逻辑索引、单调配置版本、目录重建后的写入校验 | 目录配置 CAS、同 ID 重建后拒绝旧更新和旧工作者写入。[仓储测试](../../tests/test_lifecycle_repository.py) · [端到端测试](../../tests/test_managed_knowledge_base.py) |
| 源文件、解析 JSON、Markdown 与分块 | 内容寻址及摘要校验、不可变版本、版本列表、分块分页、按产物重新分块 | 删除输入文件后重新切分，读取三个产物、核对版本引用，验收共享文件及中断上传后的回收。[端到端测试](../../tests/test_managed_knowledge_base.py) |
| 索引任务与发布 | 独立排队、状态查询、稳定任务 ID、尝试编号、真实叶子行检查、发布 CAS | 模型失败和向量缺行时继续提供旧版本，补齐索引后发布；旧工作者和已删除文档的写入被拒绝。[仓储测试](../../tests/test_lifecycle_repository.py) · [端到端测试](../../tests/test_managed_knowledge_base.py) |
| 可见性、删除及备份恢复 | 召回前限定授权叶子范围，返回前复核；逻辑删除、向量记录回收、文件引用清理及一致性快照 | 模拟检索中撤权、删文档与切版本；父块不包含被拒绝的子块；校验损坏备份、覆盖保护、共享文件、恢复后重建索引与发布。[备份测试](../../tests/test_knowledge_artifacts.py) · [端到端测试](../../tests/test_managed_knowledge_base.py) |
| 向量与中文 BM25 | 同位置向量契约、LanceDB 原生 ICU 分词、先过滤后召回 | 中文原词命中与原始分值排序，未授权记录不占 Top K，旧全文索引升级，跨实例读到新增及删除。[全文检索测试](../../tests/test_keyword_fusion.py) |
| 混合 RRF 与 HTTP 重排 | LanceDB RRF、多阶段分值、可替换重排端口、显式失败或回退 | 两路候选融合与稳定并列顺序，重排响应调整顺序；真实 HTTP 验证鉴权、超时、异常状态、非法分值和候选数量。[混合测试](../../tests/test_hybrid_pipeline.py) · [重排协议测试](../../tests/test_reranking_provider.py) |
| 精确去重、邻块、父块、来源及 MMR | 独立选择和上下文组件，保留命中和扩展块，合并同锚点重复区间 | 同区间去重、邻块遇缺口停止、预算省略记录，保留不同页的引用，关键词专有候选使用已存向量参与 MMR。[上下文测试](../../tests/test_context_selection.py) · [混合测试](../../tests/test_hybrid_pipeline.py) |
| 存储切换 | 内容、文件、向量和关键词端口独立，按请求解析内容与向量适配器 | 兼容既有仓储、向量入口，读取运行时配置变更后的后端实例。[存储测试](../../tests/test_knowledge_storage.py) |

## 复验

在仓库根目录执行。

```sh
uv sync --locked --all-extras
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests migrations scripts examples
uv run --no-sync ruff format --check src tests migrations scripts examples
uv run --no-sync mypy src
uv build
```

技术验收使用控制语料及回环 HTTP。上线效果需用目标业务语料核对词汇和编号召回、Recall/nDCG、模型排序和实际并发时的延迟与内存。恢复按目标环境的 Embedding 配置与其维度重新构建检索索引。[知识库接入](../examples/knowledge-base.md) · [检索接入](../examples/retrieval.md)
