# 存储适配与路由

`StorageRouter` 根据应用传入的配置，在同一次请求内解析内容仓储和向量索引。应用启动时注册已配置的实例，请求收到新配置后可切换其中任一后端。迁移数据、核对索引和发布目标实例由部署方负责。

| 持久化内容 | 使用的入口 |
| --- | --- |
| 知识库目录、内容版本、索引任务和发布状态 | `LifecycleRepository`，默认 [SQLiteKnowledgeRepository](../knowledge_base/adapters/sqlite.py) |
| 源文件、解析 JSON、Markdown | `ArtifactStore`，默认 [LocalArtifactStore](../knowledge_base/adapters/files.py) |
| 检索向量和全文记录 | `ManagedVectorStore`、`KeywordStore`，默认 [LanceDBVectorStore](../retrieval/adapters/lancedb.py) |
| 应用的公共状态封套 | [SQLiteStateStore](sqlite.py) 按命名空间与版本条件读写。[状态与执行控制示例](../../../docs/examples/state-execution.md) |

## 按配置选择适配器

以下代码接续知识库初始化示例，`content` 和 `index` 是已创建的实例。配置可由应用配置中心提供。

```python
from agent_components.storage.backends import StorageBinding, StorageRouter

router = StorageRouter(
    documents={"content-v1": content},
    vectors={"contracts-v1": index},
)
binding = StorageBinding.model_validate_json('{"documents":"content-v1","vectors":"contracts-v1"}')
stores = router.resolve(binding)

assert stores.documents is content
assert stores.vectors is index
```

注册名是路由用途的应用配置。`Catalog.index_id` 和 `embedding_id` 标识托管知识库核验的索引与模型绑定。切换实例前确认目标仓储、产物目录和向量记录对应同一发布版本，查询与索引使用匹配的 Embedding 配置。不同 SQLite 工作区使用独立的产物目录。后端名未注册时，`resolve` 抛 `KeyError`。[更换后端与迁移](../../../docs/examples/knowledge-base.md#换存储适配器)
