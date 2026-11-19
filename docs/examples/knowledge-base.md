# 知识库内容与可替换存储

| 包 | 公共接口 | 默认实现 |
| --- | --- | --- |
| `agent_components.knowledge_base` | `KnowledgeBase` 通过 `KnowledgeRepository` 保存文档、分块和发布指针 | [SQLite 内容适配器](../../src/agent_components/knowledge_base/adapters/sqlite.py) |
| `agent_components.retrieval` | `RetrievalPipeline` 通过 `VectorStore` 召回已授权的叶子块 | [LanceDB 向量适配器](../../src/agent_components/retrieval/adapters/lancedb.py) |
| `agent_components.storage.backends` | `StorageRouter` 通过 `StorageBinding` 选择当前请求使用的两个实例 | [存储路由](../../src/agent_components/storage/backends.py) |

SQLite 内容库使用 Python 标准库 `sqlite3`，保存规范 `Document` 和完整 `ChunkResult`，包括父子、表格和来源信息。每个文档版本保存为一个快照，发布指针与版本数据由 SQLite 事务保护。现有 `SQLiteStateStore` 用于应用状态，其表和 API 独立。

去重、MMR 和上下文扩展分别位于 `retrieval/selection.py` 和 `retrieval/context.py`。旧的 `agent_components.selection`、`agent_components.context` 和 `agent_components.retrieval.LanceDBIndex` 仍可导入。

## 入库、发布和查询

以下示例在仓库根目录执行。使用 `uv sync --locked --extra parsing --extra retrieval --extra agent`，并设置 `EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`EMBEDDING_DIMENSION`、`EMBEDDING_API_KEY`。用从未发布过该文档的数据目录运行一次。[Embedding 工厂](../../examples/providers.py)

```python
from contextlib import closing
from pathlib import Path

from agent_components.chunking import ChunkPolicy, ParentChildBuilder
from agent_components.knowledge_base import KnowledgeBase
from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository
from agent_components.parsing import DoclingParser
from agent_components.retrieval import IndexRecord, RetrievalOptions, RetrievalPipeline
from agent_components.retrieval.adapters.lancedb import LanceDBVectorStore
from agent_components.storage.backends import StorageBinding, StorageRouter
from examples.providers import compatible_embeddings

with closing(compatible_embeddings()) as embedding:
    content = SQLiteKnowledgeRepository(Path("data/knowledge/content.db"))
    vector_index = LanceDBVectorStore(
        Path("data/knowledge/vectors"),
        dimensions=embedding.dimension,
        table_name="contract_terms",
    )
    router = StorageRouter(
        documents={"content-blue": content}, vectors={"contract-blue": vector_index}
    )
    binding = StorageBinding(documents="content-blue", vectors="contract-blue")
    stores = router.resolve(binding)
    knowledge = KnowledgeBase("contracts", stores.documents)

    document = (
        DoclingParser()
        .parse(
            Path("examples/data/contract.md"),
            document_id="DEMO-2026-001",
        )
        .document
    )
    chunks = ParentChildBuilder().build_sections(
        document,
        ChunkPolicy(strategy="character", size=160),
        max_level=2,
    )
    knowledge.save_revision(document, chunks)

    # 只向量化叶子块。向量成功写入后，才更新该文档的发布指针。
    vectors = embedding.embed_batch([chunk.index_text for chunk in chunks.leaves])
    stores.vectors.upsert(
        [
            IndexRecord(
                id=chunk.id,
                document_id=chunk.document_id,
                revision_id=chunk.revision_id,
                text=chunk.index_text,
                vector=tuple(vector),
            )
            for chunk, vector in zip(chunks.leaves, vectors, strict=True)
        ]
    )
    knowledge.publish_revision(document.id, document.revision_id, expected_revision=None)

    # 示例文档即授权范围。生产从业务权限服务取得本次请求允许访问的文档 ID。
    visible = knowledge.published_chunks(document_ids=[document.id])
    result = RetrievalPipeline(stores.vectors, embedding).search(
        "验收通过后什么时候支付尾款？",
        visible_chunks=visible,
        options=RetrievalOptions(selection="mmr", parent_mode="replace"),
    )
    for window in result.context.windows:
        print(window.document_id, window.revision_id, window.index_text)
```

草稿提交后可通过 `get_revision()` 读取。SQLite 在写入前验证快照能完整读回，包含 `NaN` 或无穷大的快照会被拒绝。重复写入同一版本的相同快照幂等，内容或分块发生变化时应在分块前分配新 `revision_id`。首次发布时 `expected_revision=None`，更新或取消发布时传预期的当前版本。并发更新不匹配会抛 `RevisionConflictError`。

向量写入失败时，已发布版本保持可见。重新写入新版本的向量后，可以再次发布。`unpublish_document()` 撤下发布指针，后续读取发布快照时不包含该文档。旧内容和向量可由调用方按保留策略清理。已取得发布快照的请求继续使用自己的快照。授权文档范围用列表或元组传入，例如 `document_ids=["contract-001"]`。

## 运行时选择和更换后端

启动时将已配置实例注册为 `content-blue`、`content-green`、`contract-blue`、`contract-green` 等名称。可将绑定配置存入应用配置中心，两项可独立更换。

```json
{"documents":"content-green","vectors":"contract-blue"}
```

每次请求先加载这份 JSON，再调用 `StorageBinding.model_validate_json()` 和 `StorageRouter.resolve()`。同一请求使用解析得到的 `stores.documents`、`stores.vectors` 完成操作。未注册的配置会报错，已有请求继续使用原实例。LanceDB 适配器会在读取时检查其他实例已提交的索引版本。

- 接入 PostgreSQL 等内容库，实现 `KnowledgeRepository`，确保保存幂等、发布版本条件的原子性和发布快照的一致性。[内容端口](../../src/agent_components/knowledge_base/repository.py)
- 接入其他向量库，实现 `VectorStore`。召回前先按可见块 ID 过滤，再按相似度或距离取 Top K。MMR 请求候选向量时，从相同索引记录读取。[向量端口](../../src/agent_components/retrieval/vector_store.py)
- 同一向量集合要求块 ID 在整个集合中唯一，且使用同一向量模型、版本和预处理。外部分块的 ID 只在各知识库内唯一时，为每个知识库绑定独立向量表。切换前先准备目标内容与索引，核对已发布版本、分块 ID 和向量模型版本。`StorageRouter` 根据应用配置选择实例，数据迁移和重建索引由部署流程执行。
