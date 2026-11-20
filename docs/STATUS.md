# 当前实现边界

本次是按设计文档建立的第一阶段项目基础，不代表汇总文档的所有 P0 能力已经完成。

## 已提供实现

| 能力 | 当前实现与验证范围 |
| --- | --- |
| 公共契约 | Document、Chunk、来源、模型输入输出、抽取结果、状态封套等，不在核心导入框架 |
| 普通结构化抽取 | Document/Chunk 集合＋JSON Schema/Pydantic；Schema 与业务 validator 校验；结果/候选/错误分离 |
| 规则模型路由 | 显式别名、任务映射、默认模型与配置错误；不调用额外分类模型 |
| 统一文本调用 | 完整和流式聊天、结构化输出、系统提示词组装；聊天可按会话读取和保存版本化历史 |
| 调用日志 | 逻辑请求开始/完成/失败、模型和用量；默认无正文；内层供应商每次重试日志仍待补齐 |
| 执行控制 | 排队在内的总超时、并发、取消传播、仅显式 RetryableError 的有限重试 |
| 分块 | 六种策略、可配置规则、自动章节、表格按行、SourceMap、父子全局位置及稳定身份；详见 CHUNKING.md |
| 有界章节与重新分块 | 章节父块独立字符预算、无标题回退、跨父块表格分片；从解析产物派生新版本并保留配置与来源 |
| 流式与后处理 | 文本六策略流式、PDF 分页入口；父/邻块扩展、重叠合并、MMR、文本近重复；独立组件 |
| 解析 | TXT/CRLF；真实 Markdown/HTML/DOCX；原生元素级来源、章节和表格；PDF 默认阻止隐式下载 |
| SQLite | 状态封套、命名空间、事务版本条件写入、重启读取、Alembic 初始迁移 |
| 轻量知识库 | 目录配置、不可变版本与分块预览、原文件与解析产物、持久索引任务、索引就绪发布、撤权复核、删除与备份恢复 |
| 存储防腐层 | `LifecycleRepository`、`ArtifactStore`、`ManagedVectorStore`、`KeywordStore`，内容与向量实例通过 `StorageRouter` 按请求选取 |
| LanceDB | 向量、原生 ICU 中文分词与 BM25、范围预过滤、按块核验与清理 |
| 文本检索 | 向量／关键词／混合三种模式、LanceDB RRF、HTTP 重排与失败策略、各阶段分数、精确去重／近重复／MMR、父块和邻块 |
| AgentScope | 模型级结构化适配、原生上下文快照/压缩入口、权限判定转换、Skills 发现与内容版本 |
| 原生 SQL Storage | 独立工厂，原生模块真实导入与 SQLite 创建/查询；不启动服务 |
| LangExtract | 真实 BaseLanguageModel Provider 桥接统一模型入口，提供同步线程到异步调用的转换 |
| 评测 | Schema 指标、ir_measures Recall/nDCG/RR；不默认调用裁判模型 |
| 工程 | 固定 Python/依赖、uv.lock、CLI doctor、Ruff（含 Google 文档规则）、mypy、pytest、wheel/sdist 与 CI 配置；公开接口多行注释 |

## 仍待落地的设计项

- PDF 权重实际推理、OCR 与更完整 Office 文件矩阵和模型质量验收；元素级 SourceMap 已实现。
- **LangExtract 到任意业务 JSON Schema 的完整证据映射、字段证据、覆盖范围与部分失败。** 当前 Provider 只是模型接口桥接；Extractor 的 evidence 策略需要显式注入 EvidenceBackend，未配置时抛 UnsupportedCapabilityError，绝不冒充完整证据抽取。
- DeepEval 的统一裁判模型适配、字段与证据的完整指标及报告。
- Agent 运行入口、完整消息/工具事件协议、框架配置映射、Skills 挂载和原生失败明细。
- 人工确认请求、过期、参数绑定、持久记录、决定幂等和完整 HITL 恢复。
- 原生状态保存边界、业务工具执行记录、结果未知对账和跨框架迁移。
- 目标模型组合的实测验收、能力与输入规模路由、备用模型、实际尝试日志和全局调用/费用预算。

第 6—8 点保持设计文档中的“AgentScope 优先方向已确认、修订细节待确认”；当前只实现可明确验证的原生薄适配，未擅自扩展成业务流程。

## 检查的实际边界

集成检查使用真实安装的 Docling、Chonkie、AgentScope、SQLite、LanceDB 和 ir_measures。模型协议检查通过原生 ChatModelBase 的内存 API 实现提供响应，验证真实结构化机制和适配契约，不发送付费模型请求，也不据此宣称抽取准确率。

PDF 权重、业务 API 和模型凭据未配置，因此不宣称这些外部集成已经完成。全部公开状态以实际可调用代码与检查范围为准。

本轮已补齐此前 CHUNKING.md 的未实现与部分实现条目，按 45 个行为审查，见 [分块检查](reviews/CHUNKING_REVIEW.md)。PDF 分页调度使用真实页数与转换器替身验证，不冒充权重推理验收。其他 Agent、抽取和存储组件尚未完成相同深度的逻辑审查。

轻量知识库和检索的实现与逐项测试见 [知识库与检索验收](reviews/KNOWLEDGE_RETRIEVAL_REVIEW.md)。中文召回覆盖真实 LanceDB 和控制样本，重排协议用本地 HTTP 服务验证。业务语料的术语召回、所选模型的效果和目标环境容量由使用方按数据验收。
