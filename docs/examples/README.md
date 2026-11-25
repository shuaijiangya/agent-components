# 已开发组件接入示例

本目录对应仓库**已实现的组件接口**。示例输出真实公共契约，持久化使用真实 SQLite/LanceDB，模型接入实际 AgentScope/OpenAI 兼容协议。各命令独立调用，业务流程由接入方编排。

## 运行约定

使用 Python **3.12.14**、uv **0.13.6**，在仓库根目录执行。先选择要保留的全部依赖组，再运行对应命令：

```sh
uv sync --locked --extra parsing --extra chunking --extra storage --extra retrieval --extra agent --extra agent-storage --extra extraction --extra evaluation
uv run --no-sync python -m examples.documents --help
```

仅检查文本和字符分块时，`uv sync --locked` 即可。`uv sync` 会调整环境，后续同步时要完整列出仍需保留的 extras。

| 组件 | 可运行入口 | 依赖组 | 文档 |
| --- | --- | --- | --- |
| 文档解析／标准 PDF／原生无模型 PDF | `examples.documents parse / native-pdf` | TXT 核心；其他格式 `parsing` | [解析包 README](../../src/agent_components/parsing/README.md) · [命令行](parsing.md) |
| 六种分块／父子块／流式／重新分块 | `examples.documents chunk / parents / stream / rechunk` | 字符、分隔符核心；其余见分块文档 | [分块包 README](../../src/agent_components/chunking/README.md) · [命令行](chunking.md) |
| 父块与邻块扩展／MMR／去重 | `examples.retrieval context / select` | 核心 | [检索组件](retrieval.md) |
| 向量索引与查询 | `examples.retrieval index / search` | `retrieval` | [向量命令行](retrieval.md#嵌入式-lancedb) |
| 中文 BM25、混合召回与重排 | `RetrievalPipeline.search` | `retrieval`；按模式注入 Embedding 和重排 Provider | [检索包 README](../../src/agent_components/retrieval/README.md) · [完整检索示例](retrieval.md) |
| 知识库版本、索引任务、发布与备份 | `ManagedKnowledgeBase` | SQLite 为核心；LanceDB 需 `retrieval` | [知识库包 README](../../src/agent_components/knowledge_base/README.md) · [完整入库示例](knowledge-base.md) |
| 内容与向量存储切换 | `StorageRouter` | 核心；部署时注册相应适配器 | [存储包 README](../../src/agent_components/storage/README.md) |
| 公共状态的 CAS 保存 | `examples.state load / save` | `storage` | [状态与执行控制](state-execution.md) |
| 超时／并发／显式重试 | `examples.execution` | 核心 | [状态与执行控制](state-execution.md) |
| 模型策略路由／结构化提取 | `examples.models generate / extract` | `agent` | [模型与提取](models.md) |
| LangExtract Provider | `examples.models langextract` | `agent`、`extraction` | [模型与提取](models.md) |
| Agent 运行与工具查询 | `examples.agent_runtime` | `agent` | [Agent 接入文档](../../src/agent_components/agent/README.md) |
| 上下文／权限／Skill／原生存储适配 | `examples.runtime` | `agent`；存储需 `agent-storage` | [AgentScope 适配](agentscope.md) |
| Schema／Recall、nDCG、RR | `examples.evaluate` | Schema 核心；检索指标 `evaluation` | [评测](evaluation.md) |

源码位于 [`examples/`](../../examples/)。每个入口只处理参数、配置、公共契约转换和资源释放，关键行为仍由 `src/agent_components` 实现。示例为仓库／源码包内容，**不作为 wheel 的公共 Python API 发布**。

## 输入、输出与失败

- 文本使用 UTF-8／UTF-8 BOM；JSON 使用公共 Pydantic 契约，未知字段会被拒绝。
- stdout 输出 JSON；`stream` 和分页解析输出 JSONL；模型调用事件写 stderr。
- `--output data/xxx.json` 写文件。先写同目录临时文件，再替换结果文件；失败时保留已有结果。stdout 的 JSONL 可能已经输出部分记录，调用方必须检查退出码。
- 禁止输出覆盖输入。输出仍会替换已有同名结果文件，使用方应为每次任务选择独立目录。
- 退出码：`0` 成功，`1` 提取结果或 Schema 评测不通过，`2` 参数／依赖／后端异常。后端异常只输出 `error_type`、`code`，避免把供应商错误中的正文或凭据写进终端。
- 模型端点、密钥、模型名通过环境变量显式提供；仓库不包含密钥，不会在导入或 `--help` 时请求模型。

## 交付边界

轻量知识库和检索通过 SDK 接入。授权由业务服务提供，知识库在返回结果前复核授权与发布版本。中文 BM25、混合融合和独立重排序的测试证据见验收记录。[知识库与检索验收](../reviews/KNOWLEDGE_RETRIEVAL_REVIEW.md)

LangExtract 示例覆盖 Provider 桥接。任意 JSON Schema 的字段证据后端、审批持久化恢复及 AgentScope 跨版本迁移的状态见 [当前实现边界](../STATUS.md)。

`examples/data` 的合同为虚构资料。带 `fixture` 的向量是确定性接口验收数据，**不代表语义向量或真实检索效果**。业务向量、Tokenizer、相关性标签和模型服务应由使用方接入。

## 验证

```sh
uv run --no-sync pytest -q tests/test_examples.py tests/test_examples_integrations.py
```

测试通过真实 CLI 子进程执行；模型／Embedding 使用测试启动的本地 HTTP 兼容服务，保留 AgentScope、OpenAI 客户端、Chonkie 和 LangExtract 的真实调用路径。该验证不衡量线上模型准确率或标准 PDF 推理速度。完整验收证据见 [本轮验证记录](validation.md)。
