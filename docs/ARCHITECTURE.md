# 组件边界与 Adapter

```text
使用方的业务流程
  └─ 公共组件：解析 / 分块 / 模型 / 抽取 / 存储 / 检索 / 评测
       ├─ core：自有 Pydantic 契约、Schema 校验、协议和错误
       └─ adapters：AgentScope、LangExtract 等框架转换
```

core 不导入 AgentScope、Docling、Chonkie、LanceDB 或 LangExtract。重型依赖按可选分组提供；公共数据不要求携带框架实例。

Agent 相关 Adapter 可以接收装配方提供的原生运行。Context、Skill 等协议按能力拆分，后续后端实现同一协议；不将不支持的能力静默转换成近似行为。

StateEnvelope 的 backend/schema_version/payload 表明它是有版本的原生负载，不是跨框架直接续跑保证。真实业务的状态保存时机、审批与执行由使用方编排。

结构化抽取只接收 Document/Chunk、Schema 和选项。普通模式通过模型组件；证据模式通过显式 EvidenceBackend。组件不自动解析文件、检索 RAG、操作数据库、生成提案或调用业务 API。

知识库由 `knowledge_base.KnowledgeBase` 管理内容和发布版本，依赖 `KnowledgeRepository`。`retrieval.RetrievalPipeline` 接收已发布且已授权的分块，依赖 `VectorStore`。SQLite 和 LanceDB 适配器位于各自的 `adapters` 包，负责把公共契约转换成数据库读写。调用方通过 `StorageRouter` 按请求选择两种适配器。[存储接入与切换示例](examples/knowledge-base.md)

完整技术决策参见本目录的 design-summary.md；当前可用性参见 STATUS.md。设计与当前实现分开维护。
