# 组件边界与 Adapter

```text
使用方的业务流程
  └─ 公共组件：解析 / 分块 / 模型 / 抽取 / 存储 / 检索 / 评测
       ├─ core：自有 Pydantic 契约、Schema 校验、协议和错误
       └─ adapters：AgentScope、LangExtract 等框架转换
```

core 不导入 AgentScope、Docling、Chonkie、LanceDB 或 LangExtract。重型依赖按可选分组提供；公共数据不要求携带框架实例。

`agent_components.models` 提供模型注册、规则路由和调用日志。`models.providers` 提供 AgentScope、LangExtract、OpenAI 兼容 Embedding 和 HTTP 重排适配。并发、超时和重试由注入的通用执行器提供，分块与检索仍通过各自的 Provider 协议接入模型。[模型包接入](../src/agent_components/models/README.md)

Agent 相关 Adapter 可以接收装配方提供的原生运行。Context、Skill 等协议按能力拆分，后续后端实现同一协议；不将不支持的能力静默转换成近似行为。

StateEnvelope 的 backend/schema_version/payload 表明它是有版本的原生负载，不是跨框架直接续跑保证。真实业务的状态保存时机、审批与执行由使用方编排。

结构化抽取只接收 Document/Chunk、Schema 和选项。普通模式通过模型组件；证据模式通过显式 EvidenceBackend。组件不自动解析文件、检索 RAG、操作数据库、生成提案或调用业务 API。

知识库的 `ManagedKnowledgeBase` 组合内容仓储、文件存储、索引和 Provider。SQLite 管理文档版本和任务，发布时校验 LanceDB 叶子记录。权限由应用回调提供，检索以当前发布版本召回，返回前重新读取授权和版本。`RetrievalPipeline` 也可独立接收应用准备的授权分块。调用方通过 `StorageRouter` 按请求选择内容和向量适配器。[存储接入与切换示例](examples/knowledge-base.md)

完整技术决策参见本目录的 design-summary.md；当前可用性参见 STATUS.md。设计与当前实现分开维护。
