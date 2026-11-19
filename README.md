# Enterprise Agent Components

**Python 3.12.14 · uv 0.13.6 · 独立组件 · AgentScope Adapter**

按《Agent 组件技术选型与设计汇总》建立的 Python SDK 项目。分块相关功能已按清单完整实现并逐项验证；其余组件的实施边界见 STATUS.md，业务流程由使用方编排。

- [依赖和 Python 决策](docs/DEPENDENCIES.md)
- [已开发组件的可运行示例](docs/examples/README.md)
- [文档解析组件与完整示例](src/agent_components/parsing/README.md)
- [文档分块组件与完整示例](src/agent_components/chunking/README.md)
- [轻量知识库组件](src/agent_components/knowledge_base/README.md)
- [检索组件](src/agent_components/retrieval/README.md)
- [存储适配与路由](src/agent_components/storage/README.md)
- [非扫描 PDF：标准解析与原生无模型解析](docs/examples/parsing.md)
- [当前实现与待完成范围](docs/STATUS.md)
- [本轮验证结果](docs/VALIDATION.md)
- [组件和 Adapter 边界](docs/ARCHITECTURE.md)
- [原始设计汇总](docs/design-summary.md)
- [分块组件：接口与真实实现范围](docs/CHUNKING.md)
- [分块逐项检查：行为、代码与测试](docs/reviews/CHUNKING_REVIEW.md)
- [完整分块代码 Markdown](docs/code/CHUNKING_CODE_REVIEW.md)
- [多行注释约定](docs/COMMENTING.md)

## 安装与检查

在本目录执行。uv 会按 `.python-version` 使用 Python 3.12.14；生产代码当前限定 3.12 系列。

```sh
uv sync --locked
uv run --no-sync agent-components doctor
```

上面只安装核心和开发工具，不默认安装解析模型、Agent 或评测框架。按能力选择：

```sh
uv sync --locked --extra parsing --extra chunking
uv sync --locked --extra agent --extra extraction
uv sync --locked --extra storage --extra retrieval
```

这些是独立安装组合。后续 `uv sync` 应完整列出要保留的 extras；生产可加 `--no-dev`。

完整开发验证使用：

```sh
uv sync --locked --all-extras
uv run --no-sync ruff check src tests migrations scripts examples
uv run --no-sync ruff format --check src tests migrations scripts examples
uv run --no-sync mypy src
uv run --no-sync pytest -q
uv pip check
uv build
```

## 组件入口

| 组件 | 可调用入口 | 当前范围 |
| --- | --- | --- |
| 解析 | `agent_components.parsing.DoclingParser.parse` | TXT；真实 Markdown/HTML/DOCX；元素映射；PDF 页范围分批入口，实际推理需离线模型 |
| 分块 | `agent_components.chunking.Chunker.chunk` | 字符、递归、分隔符、Token、句子、语义；表格保持和 SourceMap |
| 父子块 | `ParentChildBuilder.build` / `build_sections` | 显式范围或 Docling 章节；全局偏移、父内序号与标题路径 |
| 重新分块 | `agent_components.chunking.Rechunker.rechunk` | 复用解析产物，派生可并存的新分块版本与追溯记录 |
| 流式 | `StreamingChunker.iter_text` / `iter_file` | 磁盘快照、有界窗口、跨窗口表格与重叠、资源硬上限 |
| 检索后处理 | `ContextExpander.expand` / `mmr_select` / `deduplicate` | 父块和邻块、上下文合并、MMR、文本近重复与可追溯结果 |
| 模型 | `agent_components.models.ModelClient.generate` | 注册别名、任务规则、显式覆盖、逻辑调用日志 |
| 抽取 | `agent_components.extraction.Extractor.extract` | Document/Chunk＋JSON Schema/Pydantic → 校验结果；无需 Agent/数据库 |
| 执行控制 | `agent_components.execution.ExecutionController.run` | 总超时、并发、取消和显式瞬时错误重试 |
| 状态 | `agent_components.storage.sqlite.SQLiteStateStore` | 命名空间与版本条件写入的 StateEnvelope |
| 索引 | `agent_components.retrieval.LanceDBIndex` | 向量和 ICU 中文全文索引、范围过滤、索引核验与删除 |
| 轻量知识库 | `agent_components.knowledge_base.ManagedKnowledgeBase` | 文件与分块版本、索引任务、发布、授权查询、删除和备份恢复。[接入示例](docs/examples/knowledge-base.md) |
| 存储切换 | `agent_components.storage.backends.StorageRouter` | 按请求独立选择已注册的内容仓储与向量存储 |
| 文本检索 | `agent_components.retrieval.RetrievalPipeline.search` | 向量和 BM25、RRF、HTTP 重排、去重或 MMR、父块与邻块扩展。[接入示例](docs/examples/retrieval.md) |
| 评测 | `evaluate_schema` / `evaluate_retrieval` | Schema 和标准检索指标，不隐式调用裁判模型 |
| AgentScope | `adapters.agentscope` | 原生模型、Context、权限、Skills 发现与 SQL 工厂 |
| LangExtract | `adapters.langextract.create_provider` | 原生模型 Provider 桥接；完整业务 Schema 证据映射仍待实施 |

接口与类型定义见 `src/agent_components/core`；这里没有内置订单、审批页面或固定业务 DAG。重型库仅在显式使用对应 Adapter 时加载。

**Evidence 策略未配置 Backend 时明确报错。** 当前 LangExtract Provider 不等于完整的长文档业务字段/证据组件；完整范围见 STATUS.md，不能把接口桥接视为该能力已经交付。

## SQLite 迁移

应用 StateEnvelope 表与 AgentScope 原生表分别管理。开发检查可用 `create_tables=True` 创建应用表；发布时使用迁移：

```sh
mkdir -p data
uv run --extra storage alembic upgrade head
```

可用 `AGENT_COMPONENTS_DATABASE_URL` 设置迁移目标，使用 SQLAlchemy 异步 SQLite URL。`SQLiteStateStore.save(..., expected_revision=0)` 表示新建，已有记录必须使用当前 revision，冲突抛出 RevisionConflictError。

原生 `AsyncSQLAlchemyStorage` 使用 `agent-storage` 组，其模块导入需要 Service Python 依赖，但工厂不会启动任何 HTTP/Redis 服务。该兼容问题及 CPU PyTorch 来源见 DEPENDENCIES.md。

## 后续实施入口

当前可用功能和验收范围见 [STATUS.md](docs/STATUS.md)。后续方向包括 LangExtract 证据字段映射、Agent 工具事件和 HITL 持久恢复。
