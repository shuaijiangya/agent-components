# 轻量知识库组件

`ManagedKnowledgeBase` 管理文档产物、分块版本、索引任务和发布状态。默认用 SQLite 保存内容和状态，本地目录保存文件，嵌入式 LanceDB 保存检索副本。业务应用决定何时解析、索引、审批和发布，并提供实时授权范围。

| 操作 | SDK 入口 |
| --- | --- |
| 创建、查询、更新、删除目录 | `SQLiteKnowledgeRepository` 的 `create_catalog`、`get_catalog`、`list_catalogs`、`update_catalog`、`delete_catalog` |
| 保存草稿 | `save_parsed(parsed, source=原文件字节, policy=策略)`，可传 `section_options` 生成有界章节父子块 |
| 读取与预览 | `list_documents`、`list_revisions`、`get_revision`、`chunk_page`、`read_artifact` |
| 调整分块 | `rechunk_revision` 从保存的解析产物派生新版本，并记录前一版本 |
| 构建和恢复索引任务 | `enqueue_index`、`run_index`、`get_index_job`、`list_index_jobs`、`reset_index` |
| 发布、撤下与查询 | `publish_revision`、`unpublish_document`、`search` |
| 删除、清理与备份 | `delete_document`、`purge_deleted`、`collect_orphan_artifacts`、`backup_workspace`、`restore_workspace` |

## 创建知识库并首次发布

以下示例在仓库根目录运行。先安装 Markdown 解析、LanceDB 和示例 Embedding 客户端依赖。

```sh
uv sync --locked --extra parsing --extra retrieval --extra agent
```

设置 `EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`EMBEDDING_DIMENSION`、`EMBEDDING_API_KEY`。示例使用新的 `data/knowledge` 工作区与仓库中的合同 Markdown。应用应为每个知识库绑定专用 LanceDB 表，并用版本化的 `embedding_id` 标识模型和预处理配置。[Embedding 工厂](../../../examples/providers.py)

```python
from contextlib import closing
from pathlib import Path

from agent_components.chunking import ChunkPolicy, SectionOptions
from agent_components.knowledge_base import Catalog, ManagedKnowledgeBase
from agent_components.knowledge_base.adapters.files import LocalArtifactStore
from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository
from agent_components.parsing import DoclingParser
from agent_components.retrieval import LanceDBIndex

from examples.providers import compatible_embeddings

root = Path("data/knowledge")
content = SQLiteKnowledgeRepository(root / "content.db")
artifacts = LocalArtifactStore(root / "artifacts")

# 部署时的稳定配置。升级模型或预处理时，在新的索引空间重建。
index_id = "contracts-v1"
embedding_id = "contracts-embedding-v1"
if content.get_catalog("contracts") is None:
    content.create_catalog(Catalog("contracts", "采购合同", index_id, embedding_id))

path = Path("examples/data/contract.md")
document_id = "DEMO-2026-001"
parsed = DoclingParser().parse(path, document_id=document_id)

with closing(compatible_embeddings()) as embedding:
    index = LanceDBIndex(
        root / "vectors", dimensions=embedding.dimension, table_name="contracts_v1"
    )
    kb = ManagedKnowledgeBase(
        "contracts",
        content,
        artifacts,
        index,
        embedding,
        index_id=index_id,
        embedding_id=embedding_id,
    )

    draft = kb.save_parsed(
        parsed,
        source=path.read_bytes(),
        policy=ChunkPolicy(strategy="character", size=160),
        section_options=SectionOptions(max_level=2, max_parent_chars=800),
    )
    revision_id = draft.document.revision_id
    print(kb.chunk_page(document_id, revision_id, kind="leaf", limit=10))
    print(kb.read_artifact(document_id, revision_id, "markdown").decode("utf-8"))

    job = kb.enqueue_index(document_id, revision_id)
    kb.run_index(job.id, batch_size=32)
    # 首次发布传 None。审批更新时传审批方看到的当前版本 ID。
    kb.publish_revision(document_id, revision_id, expected_revision=None)
```

## 更新已发布文档的分块

以下代码接续首次发布示例，追加在上述示例的 `with closing(compatible_embeddings())` 代码块内。`revision_id` 是当前已发布版本。业务应用可把创建草稿、构建索引和发布分成独立操作。

```python
from agent_components.chunking import ChunkPolicy, SectionOptions

draft = kb.rechunk_revision(
    document_id,
    revision_id,
    ChunkPolicy(strategy="character", size=120),
    section_options=SectionOptions(max_level=2, max_parent_chars=600),
)
next_revision = draft.document.revision_id
assert draft.trace.previous_revision_id == revision_id

preview = kb.chunk_page(document_id, next_revision, kind="leaf", offset=0, limit=10)
print([chunk.text for chunk in preview])

job = kb.enqueue_index(document_id, next_revision)
kb.run_index(job.id, batch_size=32)

# 审批方用其看到的发布版本作为条件提交，防止覆盖并发发布。
kb.publish_revision(document_id, next_revision, expected_revision=revision_id)
```

重复提交相同的草稿会得到同一版本和索引任务 ID。草稿读取与分块预览可在索引开始前进行。发布核验该版本的任务状态和所有叶子索引记录。若索引失败或预期版本发生变化，既有发布指针继续生效。

## 运行与恢复

- 工作进程可通过 `list_index_jobs(status="queued")` 分页发现任务。失败任务可以再次执行。进程在 `running` 时退出，可按当前尝试编号调用 `reset_index`，再重新领取；旧尝试无法覆盖新状态。
- 查询传入 `authorized_document_ids` 回调；`document_ids` 可进一步缩小范围。组件在召回前限定授权发布版本，在返回前复核。权限变化时抛 `RevisionConflictError`，调用方可按最新授权重试。预览草稿和下载产物由业务管理端鉴权。[检索配置与结果](../retrieval/README.md)
- `delete_document(..., expected_revision=...)` 先撤下发布并记录删除标记。确认需要清理后调用 `purge_deleted`，可重试。仍被其他文档引用的文件会保留。中断上传的无引用文件可由 `collect_orphan_artifacts` 回收。
- `backup_workspace` 归档同一 SQLite 工作区的数据库及引用产物，路径需空闲。恢复用独立数据库和产物目录，返回需要重新索引的已发布版本清单。重建成功后按清单重新发布。[恢复示例](../../../docs/examples/knowledge-base.md#备份与恢复)

业务方可按请求独立选择内容和向量适配器。[存储路由与切换](../storage/README.md) · [验收项目与测试](../../../docs/reviews/KNOWLEDGE_RETRIEVAL_REVIEW.md)
