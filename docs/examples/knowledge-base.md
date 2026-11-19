# 轻量知识库开发接入

使用 `ManagedKnowledgeBase` 保存文件和分块、执行独立索引任务、发布版本和查询。SQLite、文件目录和 LanceDB 均在应用进程内使用。解析与分块仍可独立调用。[设计选型](../design-summary.md#4-轻量知识库与检索组件)

| 接口 | 持久化内容 | 默认实现 |
| --- | --- | --- |
| `LifecycleRepository` | 知识库配置、文档与分块版本、文件引用、发布指针、索引任务、删除标记 | [SQLiteKnowledgeRepository](../../src/agent_components/knowledge_base/adapters/sqlite.py) |
| `ArtifactStore` | 源文件、完整 ParsedDocument JSON（含 Docling 原生结构）、Markdown | [LocalArtifactStore](../../src/agent_components/knowledge_base/adapters/files.py) |
| `ManagedVectorStore`、`KeywordStore` | 叶子块的向量和全文文本，以及用于校验的文档和版本标识 | [LanceDBVectorStore](../../src/agent_components/retrieval/adapters/lancedb.py) |
| `EmbeddingProvider`、`RerankerProvider` | 调用方配置的模型能力 | [Embedding 工厂](../../examples/providers.py) · [HTTPReranker](retrieval.md#重排序与分数) |

## 创建、入库、发布和查询

从知识库包 README 执行创建目录、保存解析草稿、预览分块、索引和首次发布。检索包 README 提供关键词查询和混合重排示例。[知识库首次发布](../../src/agent_components/knowledge_base/README.md#创建知识库并首次发布) · [检索包 README](../../src/agent_components/retrieval/README.md)


示例授权函数只为这一个测试文档返回授权。线上每次调用从权限服务读取当前访问范围。组件在召回前限定已发布叶子，再在返回前读取一次权限与发布状态。范围或版本变化时抛 `RevisionConflictError`。可选 `allowed_chunk` 需要对整个块正文与重复表头作出授权判断。任意子块不可见时，托管入口会隐藏父块，仍可返回获准的叶子和邻块。审阅草稿、读取文件和更改发布指针属于管理操作，应用在调用各入口前鉴权。[检索结果与预算](retrieval.md)

## 更新、重试与删除

- 用相同业务文档 ID 提交更新后的文件，`save_parsed` 保存新版本，原文件 SHA-256 必须和解析记录匹配。相同解析产物和分块配置重复提交得到同一草稿版本。用 `list_documents` 查当前发布版本，用 `list_revisions`、`get_revision` 和 `chunk_page` 查历史。
- 只改分块时调用 `kb.rechunk_revision(document_id, revision_id, new_policy)`。调用会从仓储读取解析产物与源文件校验摘要，沿用原解析结果。新版本的 `trace.previous_revision_id` 记录分支来源。
- 工作者领取并执行 `run_index(job_id)`，任务状态为 `queued`、`running`、`succeeded` 或 `failed`。重复 `enqueue_index` 返回稳定任务 ID。失败任务可直接重试，已成功的执行直接返回当前任务。`list_index_jobs(status="queued")` 可按状态分页发现任务。若进程在运行中退出，管理员用 `reset_index(job_id, expected_attempt=…)` 重排，尝试编号会拒绝旧工作者写入。
- 新版本索引成功且叶子行全部核验通过后，才可以 `publish_revision(..., expected_revision=旧版本)`。失败或比较版本冲突时，已有发布指针继续生效。显式重建当前已发布版本前，先 `unpublish_document`，或准备新的版本和独立索引后切换。
- `delete_document(document_id, expected_revision=…)` 原子撤下发布并保留删除标记。`purge_deleted(document_id)` 清理该文档各版本的索引记录和无人引用的文件。可以重复调用，同一仓储中的其他引用继续有效。用 `collect_orphan_artifacts()` 清理中断上传产生的无引用文件。已经生成的备份和 LanceDB 历史版本按部署保留策略管理。
- 用 `create_catalog`、`get_catalog`、`list_catalogs`、`update_catalog` 和 `delete_catalog` 管理目录。更新名称和删除目录使用 `expected_version`。绑定创建后固定，清理目录的文档和待删除文件后才可删除目录。

## 备份与恢复

`kb.backup_workspace(Path("backup/contracts-20261119.zip"))` 备份该 SQLite 工作区的所有知识库和其引用的文件，要求新的归档路径。SQLite 文件可以由多个知识库共享，产物目录归这个工作区独占。可迁移到挂载支持硬链接的持久目录。同一环境中移动数据时使用备份恢复到新路径和独立产物目录。[备份实现](../../src/agent_components/knowledge_base/backup.py)

```python
from pathlib import Path

from agent_components.knowledge_base import restore_workspace
from agent_components.knowledge_base.adapters.files import LocalArtifactStore

plan = restore_workspace(
    Path("backup/contracts-20261119.zip"),
    Path("recovered/content.db"),
    LocalArtifactStore(Path("recovered/artifacts")),
)
for entry in plan:
    print(entry.knowledge_base_id, entry.document_id, entry.revision_id)
```

恢复先校验数据库、归档和文件摘要，再安装到新路径。备份覆盖内容和产物，LanceDB 向量和全文索引由恢复后的内容重建。返回的清单列出备份时已发布的托管版本。对恢复后的每个知识库绑定新索引，按清单分别执行 `enqueue_index`、`run_index`、`publish_revision(..., expected_revision=None)` 后开放查询。对旧版 `KnowledgeBase` 命名空间按应用原有流程恢复其索引并发布。

## 换存储适配器

按运行时配置注册两个端口实例，每个请求解析同一份 `StorageBinding`。例如把内容指向 `content-green`，把索引指向 `contracts-green`。切换前核对目标库版本、产物和索引齐备，已有请求继续持有原实例。知识库与检索可单独使用，沿用原有 `KnowledgeBase` 的应用负责自己的索引和发布协议。[StorageRouter](../../src/agent_components/storage/backends.py)

```python
from agent_components.storage.backends import StorageBinding, StorageRouter

router = StorageRouter(
    documents={"content-green": content},
    vectors={"contracts-green": index},
)
stores = router.resolve(StorageBinding(documents="content-green", vectors="contracts-green"))
```

- 换内容库实现 `LifecycleRepository`，保留不可变快照、同事务发布比对和尝试编号。换向量库实现 `ManagedVectorStore`，按可见 ID 先过滤后取 Top K，并支持按块读取及清理。保持索引的 Embedding 指纹一致。
- 默认 LanceDB 将向量和全文文本写入同一记录。主动注入独立 `keyword_index` 时，由它所在的部署写入、验证和清理其副本。
- SQLite 事务内完成发布验证和短时索引写入，Embedding 请求在写事务外发出。批量索引任务按文档暂存向量，检索按授权文档读取当前快照。按实际文档和并发规模验证内存、数据库写锁等待与检索延迟。
