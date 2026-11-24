# Agent 组件技术选型与设计汇总

更新时间：2026-11-16  
文档版本：0.1（当前选型汇总）  
目标：为业务开发者提供可独立调用、可组合、可替换后端的 Python 组件及 Markdown 接入文档。

> 本文记录技术决策和待实施边界，不代表功能已经实现或通过生产验收。基础文档与知识库选型、Agent 第 1—5 点及第 9 点已确认；第 6—8 点的 AgentScope 优先与 Adapter 隔离方向已确认，具体修订方案待最终确认。

## 1. 总体原则

1. **以组件交付。** 每个组件明确输入、输出、配置、错误和扩展点；具体业务流程、调用顺序和业务规则由使用方组织。
2. **优先复用开源实现。** 文件解析使用 Docling，分块使用 Chonkie，Agent 相关能力优先贴合 AgentScope 封装。
3. **公共契约与框架实现隔离。** 我们定义消息、Chunk、Schema、工具、事件和结果等公共契约；框架对象在 Adapter 内转换。
4. **无关能力不成为前置条件。** 分块无需先创建知识库；纯 Chunk 管理无需索引就绪；结构化抽取无需创建 Agent、会话或数据库。
5. **Agent 相关能力允许复用已注入的运行实例。** 上下文压缩、Skills 挂载和人工确认可使用 AgentScope 运行，不为抽象而重写整套运行机制。
6. **轻量部署。** 优先 Python SDK、SQLite、本地持久目录和 LanceDB Embedded，不要求部署模型网关或独立检索数据库服务。
7. **明确版本与来源。** 文档、分块、检索结果、抽取证据和执行记录都应保留适用的版本与配置标识。

模型接入采用 AgentScope 原生接口，不再使用 LangChain。具体 LLM、Embedding、Reranker 及部署方式另行配置，不在本文指定模型。

## 2. 基础组件与技术选择

| 组件 | 技术选择 | 核心职责 | 需要封装的部分 |
| --- | --- | --- | --- |
| 文档解析 | [Docling](https://github.com/docling-project/docling) | 将文件转换为统一文档结构及可用来源信息 | 格式路由、参数、产物、错误、格式验收和来源适配 |
| 通用分块 | [Chonkie](https://docs.chonkie.ai/oss/chunkers/overview) | 递归、句子、Token、语义等分块能力 | 统一策略、字符/分隔符适配、中文配置、来源与长度校验 |
| 章节与父子块 | Docling 结构＋Chonkie＋自有 ParentChildBuilder | 按结构生成父块，再在父块内分子块 | 章节聚合、边界、层级、ID、父子与相邻关系 |
| 内容管理 | SQLite＋本地持久目录 | 管理知识库、文档、版本、Chunk、任务及文件产物 | Repository、迁移、发布、删除、幂等、备份和清理 |
| 检索存储 | [LanceDB Embedded](https://github.com/lancedb/lancedb) | 向量、FTS/BM25、过滤、混合检索与 RRF | 索引生命周期、配置版本、权威记录校验和检索接口 |
| Embedding / 重排序 | 独立 Provider 接口 | 接入指定 SDK/API 或现有实现 | 批量对应、配置指纹、超时、输出校验、日志和失败策略 |
| 模型调用 | [AgentScope](https://github.com/agentscope-ai/agentscope) 原生模型接口 | 异步调用、流式、工具参数和结构化输出 | 模型注册表、规则选模、能力检查、统一调用和生命周期 |
| 模型日志 | 统一入口＋AgentScope 事件/中间件＋Python logging | 记录实际调用与运行事件 | JSON 字段、关联、脱敏、轮转、用量和各调用路径覆盖 |

### 2.1 当前不采用 LlamaIndex 的理由

当前目标是独立解析、分块和检索组件。直接复用 Docling、Chonkie 与检索后端，可以保持公共契约和生命周期清晰，减少上层框架集成层。

这不表示 LlamaIndex 能力或精度更差。它已有 Docling、Chonkie 适配及层级节点能力；增加它仍不能自动解决自有版本、来源映射和发布规则。若未来需要统一节点体系或原生层级节点管理，可重新评估。

## 3. 文档解析与分块组件

### 3.1 输入输出

| 能力 | 输入 | 输出 |
| --- | --- | --- |
| 解析 | 文件及解析配置 | 统一 Document、Docling 原生结构/JSON、可用来源；Markdown 可作为预览产物 |
| 分块 | Document 或文本＋分块策略 | 标准化 Chunk 集合、策略与来源元数据 |
| 重新分块 | 已有解析产物＋新分块配置 | 新版本 Chunk，无需重复解析原文件 |

以下为拟定公共契约，不是已经交付的库 API：

```text
parse(file, options) -> ParsedDocument
chunk(document_or_text, policy) -> ChunkResult
```

### 3.2 分块策略与责任

| 策略/能力 | 实施方式 | 当前范围 |
| --- | --- | --- |
| 按字符、Token、句子 | Chonkie 适配及必要的计数封装 | 首期 |
| 按分隔符、递归规则 | Chonkie 递归能力＋显式规则配置 | 首期 |
| 按章节/小节 | 基于 Docling 结构聚合，超长内容按明确策略切分 | 首期 |
| 表格处理 | 复用可用结构与分块能力，保留表头及来源 | 首期，支持范围需验收 |
| 父子块 | 自有 ParentChildBuilder，父块内独立分子块 | 首期 |
| 语义分块 | Chonkie SemanticChunker 与相应 Provider | 后续增强 |

**Chonkie 的递归分块层级不是父子块树。** 父子关系、文档顺序和检索回填由我们封装，不能将它们视为 Chonkie 原生交付能力。参考 [RecursiveChunker](https://docs.chonkie.ai/oss/chunkers/recursive-chunker#return-type)。

父块优先采用有界章节或小节，无可靠标题时使用可配置的大块。子块在各自父块内生成，保证包含关系。字符、Token、重叠、分隔符及章节层级等参数必须明确单位和实际行为。

解析与来源映射需验证真实中文、表格和阅读顺序。页码、坐标或可靠映射不可用时明确为空或降低来源精度，不虚构定位。

## 4. 轻量知识库与检索组件

### 4.1 存储分工

| 存储 | 保存内容 | 部署方式 |
| --- | --- | --- |
| SQLite | 知识库、文档、版本、Chunk、任务、索引构建和发布记录 | 应用访问本地持久文件 |
| 本地持久目录 | 原文件、Docling JSON、Markdown 及其他产物 | 与数据库关联保存引用 |
| LanceDB Embedded | 向量、全文检索及必要的检索副本元数据 | 应用进程内使用，无需独立数据库进程 |

SQLite 是内容版本、删除及发布状态的权威来源。SQLite、文件和 LanceDB 之间不假设存在跨存储事务。

### 4.2 知识库管理能力

- 创建、查询和配置知识库；添加、更新、删除文档。
- 查看解析产物、文档版本和分页 Chunks；按配置重新分块。
- 内容处理与索引构建分开：内容就绪即可获取 Chunks，索引另行构建、查询和重试。
- 新索引构建完成后切换发布版本，期间可继续查询旧的已发布版本。
- 删除先停止可见性，再回收文件与检索副本；查询返回前按权威记录核对版本和权限范围。
- 持久化任务状态与稳定 ID；备份覆盖数据库及引用产物。

### 4.3 检索能力

对外提供可配置的检索接口，输入查询、知识库范围、过滤条件及检索策略，输出带版本、来源和各阶段分数的 SearchHit / ContextBlock。

| 能力 | 实现 | 范围 |
| --- | --- | --- |
| BM25 全文召回 | LanceDB FTS | 首期；中文分词与领域术语需验收 |
| 向量召回 | EmbeddingProvider＋LanceDB | 首期；实际 Provider 另定 |
| 混合融合 | 复用 LanceDB RRF | 首期 |
| 重排序 | Reranker Provider | 首期提供真实接入与明确失败策略 |
| 父块回填 | 小块命中后读取关联父块 | 首期，自有封装 |
| 相邻块扩展 | 同文档版本的顺序/邻接关系 | 首期，自有封装 |
| 精确去重 | Chunk、父块及重复来源区间处理 | 首期，自有封装 |
| MMR | 候选相关性与相似性约束 | 后续增强，属于检索层 |

上述处理阶段按策略配置，不代表使用方必须采用固定业务流程。父块和相邻扩展遵守章节、版本及长度预算，保留命中子块与扩展上下文的区别。参考 [LanceDB 全文检索](https://docs.lancedb.com/search/full-text-search)与[混合检索](https://docs.lancedb.com/search/hybrid-search)。

## 5. 模型调用、规则路由与日志

### 5.1 模型调用组件

默认通过 AgentScope 原生模型接口访问模型，公共请求/响应与供应商对象隔离。使用方配置模型别名、供应商、凭据来源和能力。

路由支持任务类型、显式模型别名、所需能力、输入规模和配置的失败回退。默认采用进程内确定性规则，不额外调用一个模型来选择模型。AgentScope 内置模型路由中间件中的分类器式路由不能直接等同于这套规则。

不引入独立模型网关。Embedding 与重排序通过各自 Provider 直接接入；Embedding 配置不能像 Chat 模型一样自由切换，同一索引必须保持匹配的向量空间。

### 5.2 日志与执行约束

- 使用结构化标准输出或本地滚动 JSONL，明确实际落点和保留策略。
- 记录 request/call/attempt 标识、适用的 run/job/文档版本、策略与模型、耗时、状态、重试、回退和用量。
- 正文记录可配置；凭据不写入日志。未提供的用量为空，估算另行标识。
- 普通模型、结构化输出、LangExtract、Embedding 与重排序均需覆盖，不能假定都触发相同 Agent 回调。
- SDK、模型层及外层重试明确唯一归属和总尝试上限，避免重试相乘。
- 流式输出中途失败明确结束当前尝试，不静默拼接另一个模型的输出。

## 6. 独立结构化数据与证据抽取组件

**公共能力：给定 Chunk 或完整 Document，以及目标结构，返回结构化数据。**

```text
extract(content, schema, options) -> ExtractionResult
```

组件入口为 `agent_components.support.Extractor`。配置、证据字段及运行示例见 [抽取包 README](src/agent_components/support/README.md)。

### 6.1 输入与输出

| 项目 | 约定 |
| --- | --- |
| content | 单个 Chunk、多个 Chunk 或完整 Document 的实际文本；可带标识、版本和来源映射 |
| schema | 目标 JSON Schema；也可接受 Pydantic 模型。普通示例 JSON 不自动等同于完整 Schema |
| options | 抽取说明、结构化/证据策略、可选示例、模型、执行与证据配置 |
| data | 符合调用方 Schema 的 JSON，其字段和层级由调用方决定 |
| evidence | 可选，按最终字段的 JSON Pointer 关联原文、字符区间、对齐状态和可用来源 |
| status / errors / warnings / meta | 处理状态、字段/分段错误、范围、模型配置、用量和耗时，与 data 分开 |

没有有效数据时不伪造字段。缺失值只有在 Schema 允许时使用 null；不合规候选与已验证 data 分开。部分输入失败必须明确报告，不能伪装成完整成功。

### 6.2 技术实现

| 子能力 | 技术 | 必须封装 |
| --- | --- | --- |
| 普通结构化抽取 | AgentScope 模型级 generate_structured_output | 提示与 Schema 适配、结果与错误归一化 |
| 长文档与证据抽取 | [LangExtract](https://github.com/google/langextract) | 目标业务结构映射、分段结果合并、字段证据关联 |
| Pydantic 模型与固定请求/结果 | Pydantic | 类型、约束、明确归一化规则和校验错误 |
| 动态 JSON Schema | 标准 JSON Schema 验证器 | Schema 检查、支持范围和实例校验；不假定 Pydantic 自动覆盖任意 Schema |
| LangExtract 模型访问 | 自定义 Provider → 统一模型组件 | 输出封套、Schema、同步/异步桥接、批量顺序、日志与预算 |
| 页码与坐标 | 可选 SourceMap / Docling 适配 | 输入文本位置回溯原文元素，声明精度与缺失信息 |

LangExtract 与 AgentScope 的模型接口不同，Provider 适配需要开发和验收。LangExtract 内部的 extraction 结构也不自动等于任意嵌套业务 JSON，需明确映射与支持范围。参考 [AgentScope 结构化输出](https://github.com/agentscope-ai/agentscope/blob/v2.0.11/src/agentscope/model/_base.py)与 [LangExtract Provider](https://github.com/google/langextract/blob/main/langextract/providers/README.md)。

### 6.3 组件边界

- 不要求 Agent、Toolkit、会话、知识库或数据库；模型与执行能力通过配置/接口注入。
- 不自动解析原始文件、查询 RAG、生成业务流程、审批或执行后续操作。
- 对传入完整文档处理完整目标范围；对传入 Chunk 只声明覆盖该 Chunk，不推断其等于全文。
- 长文档优先复用 LangExtract 内部分段和多轮策略；Chonkie 继续用于独立的知识库分块。
- 字符区间对应固定的输入文本。没有 SourceMap 时仍可返回文本内证据，但不自动拥有 PDF 页码和坐标。
- 精确、模糊及未定位状态必须保留；Schema 合规、位置可验证和事实正确分别判断。
- 基础调用不默认持久化任务；HTTP、后台任务和状态保存作为可选适配。

## 7. Agent 相关第 1—9 点

以下编号沿用技术确认顺序，与本文章节编号无关。

| 编号 | 能力 | 选择与主要复用 | 需封装的功能 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | Agent 运行/编排 | AgentScope，优先 SDK 嵌入 | 配置与工具装配、运行入口、公共消息和事件、Adapter | 已确认 |
| 2 | 执行控制与容错 | 原生循环上限、模型重试/备用模型、Token 预算＋asyncio＋工具 I/O 用 Tenacity | 统一期限、取消、并发、次数、日志和重试归属 | 已确认 |
| 3 | 工具接入 | Toolkit、FunctionTool / ToolBase、Pydantic；原生 MCPClient 按需 | 工具契约、实际校验、可信上下文、公共结果与错误、执行策略接入 | 已确认 |
| 4 | 状态持久化与恢复 | AgentState＋AsyncSQLAlchemyStorage＋SQLite/aiosqlite；Alembic 迁移 | 存取、可选运行/工具记录、检查点协调、恢复判定、版本与隔离 | 已确认 |
| 5 | 结构化抽取 | 本文第 6 节的独立组件 | Chunk/Document＋Schema 契约、模型与 LangExtract 适配、证据和校验 | 已确认 |
| 6 | 上下文管理 | 原生 ContextConfig、压缩、Offloader | 公共配置/快照/结果、原生映射、错误和事件 | 方向已确认，修订细节待确认 |
| 7 | Skills | 标准 Skill 包、LocalSkillLoader、Toolkit / SkillViewer | 稳定 ID、版本与资源摘要、注册/挂载、资源和错误 | 方向已确认，修订细节待确认 |
| 8 | 人工确认 / HITL | PermissionEngine、原生确认事件、挂起与恢复 | 公共请求/决定、可信身份、参数与版本绑定、持久映射和幂等 | 方向已确认，修订细节待确认 |
| 9 | 质量评测 | DeepEval＋ir_measures＋确定性指标 | 样本与标注、指标配置、模型 Adapter、错误与聚合报告 | 已确认 |

### 7.1 执行控制、工具与状态的共同约定

- 总期限包含排队、重试等待和流式读取；循环轮数不等同于实际模型尝试次数。
- Token 预算中间件的累计与收尾机制不直接等于严格费用硬上限，辅助调用和重试也需要统计。
- 查询工具可按规则有限重试；写入工具在下游幂等与结果核对契约明确后才开放重试。
- 超时和取消不等于远端操作撤销。已派发但结果未知时先核对，不直接当成未执行重放。
- 工具 Schema 用于描述参数，执行前仍要显式验证；可信用户、租户和凭据来自调用方上下文。
- 业务 API 可直接包装 SDK/客户端，不强制改成 MCP。STDIO MCP 涉及子进程，HTTP MCP 连接已有服务。
- 状态组件提供保存、加载、查询与恢复判定；何时保存及恢复由使用方或显式启用的 Adapter 决定。
- 保存会话不等于任意步骤事务恢复；工具执行记录与外部结果核对决定是否可以安全继续。
- 原生多次存储调用与外部业务 API 不属于同一事务，恢复协调需要处理状态保存之间的差异。

### 7.2 第 6—8 点的最新修订

**以 AgentScope 原生实现为默认，通过 Adapter 包装，不为框架无关重新实现运行机制。**

- 第 6 点取消首期独立 ContextBuilder/自写裁剪和摘要引擎，tiktoken 不作为强制依赖。复用已注入运行的原生上下文管理；计数精度按实际实现说明。
- 第 7 点直接复用原生加载、Toolkit 挂载和指令读取。加载本身不自动运行脚本，Skill 中的工具声明不自动授予权限。
- 第 8 点直接复用原生权限与 HITL。请求需关联原工具调用、参数、版本和有效期；人工决定经验证后，由使用方显式触发框架恢复。页面、通知、审批人和组织授权由使用方接入。
- 需要跨请求或重启后的审批保证时，显式接入持久记录；原生 ASK 事件本身不是完整审批平台。

### 7.3 第 9 点的评测范围

| 评测内容 | 实现 |
| --- | --- |
| JSON 可解析、Schema 合规 | 复用 Pydantic / JSON Schema 校验，生成报告 |
| 字段/实体 precision、recall、F1 | 自有确定性指标，明确归一化、数组、重复及空值匹配规则 |
| 证据区间、原文匹配、字段引用覆盖 | 自有确定性指标；语义支持与位置正确分开 |
| Recall@K、nDCG、MRR | [ir_measures](https://ir-measur.es/en/latest/getting-started.html)，需要相关性标注 |
| 回答相关性、忠实度、自定义评分规则 | [DeepEval](https://github.com/confident-ai/deepeval)，显式配置裁判模型 |
| 工具选择与参数 | 评估传入调用记录，不执行真实工具 |

评测输入为已有实际输出、参考标注和指标配置，输出分项报告；缺少必需标注明确为错误或不适用。裁判模型通过自定义 Adapter 接统一模型入口；确定性指标无需裁判模型，不要求云评测账号。

## 8. AgentScopeAdapter 与框架更换

```text
使用方
  └─ 自有组件接口、配置、消息、工具和事件
       └─ AgentScopeAdapter
            └─ AgentScope 原生实现
```

| 层级 | 责任 |
| --- | --- |
| 公共契约 | 自有 RuntimeRef / SessionRef、消息内容块、工具 Schema、ContextPolicy / Result、SkillDescriptor、ApprovalRequest / Decision 和错误 |
| Adapter | 框架对象、配置、调用、回调、事件、错误及状态的转换 |
| 原生框架 | 推理/工具循环、上下文压缩、Skills 接入、工具权限与人工确认等实际能力 |

首期只实现 AgentScope 后端，可按 ContextBackend、SkillBackend、ApprovalBackend 等能力拆分协议，不预先实现所有框架或一个巨大的统一运行时。

更换框架时增加相应 Adapter，并验证能力映射。新后端不支持的策略应明确拒绝或声明不支持，不静默忽略；后端专有扩展明确标注可移植范围。

标准 Skill 包、JSON Schema、业务工具函数和通用数据可继续复用。框架内部 State、历史检查点、运行中任务和待确认调用不能假定直接跨框架续跑，需导出重建或专门的版本迁移。

## 9. 首期交付、增强与后续选型

### 9.1 首期实施清单

| 范围 | 必须落地的内容 |
| --- | --- |
| 公共契约 | Document、Chunk、来源、检索、抽取、模型、工具及 Agent 事件/结果；配置与错误约定 |
| 解析与分块 | 格式验收、产物、基础策略、章节/表格、父子/邻接、来源映射及重新分块 |
| 知识库 | 内容与版本管理、任务/幂等、文件管理、索引构建/发布、删除与备份 |
| 检索 | BM25、向量、RRF、真实重排 Provider、父块/相邻扩展、精确去重及结果来源 |
| 模型与日志 | 原生模型接入、规则选模、能力校验、实际尝试日志和失败/取消 |
| 抽取 | 独立接口、普通与证据策略、LangExtract Provider、JSON 结构适配、证据验证及错误 |
| Agent 基础 | 已确认的运行、控制、工具与存储组件；第 6—8 点按修订方案确认后实施原生包装 |
| 评测与文档 | 真实样本、确定性与可选语义指标、报告、各组件文档及可选 Adapter 说明 |

首期必须真实接入并验收配置的 Provider；仅提供空接口或复述开源能力不算交付。

### 9.2 P1：已记录的增强

- MMR 多样性选择、近似去重和信息覆盖评测。
- Chonkie 语义分块及与规则分块的对比。
- 多层父子结构、按需合并和更精细的上下文选择。
- 标题/章节/正文多字段、领域词汇和中文检索配置增强。
- 可配置融合权重、每文档上限及相同父块聚合。
- Embedding 增量复用与缓存，包含配置和权限隔离。
- 为已验证 SDK 增加 HTTP 薄适配；提取任务、缓存和断点恢复按需另封装。

### 9.3 P2 与尚未确认的技术方向

以下仅保留扩展点，不代表已经选型或默认实施：

- 更多解析器、对象存储、PostgreSQL 或服务型检索后端。
- 查询改写、多查询、HyDE、额外稀疏/多向量召回和 GraphRAG。
- 跨会话长期记忆、多 Agent 协作策略。
- 浏览器自动化、代码执行及沙箱。
- 分布式追踪、持久任务队列、自动恢复与调度。
- 前端事件协议、A2A 等跨 Agent 协议。
- 其他 Agent 框架 Adapter 和历史状态迁移。

## 10. 统一数据、文档和验收

### 10.1 关键数据约定

| 对象/字段 | 要求 |
| --- | --- |
| DocumentRevision | 内容摘要、解析/分块配置与产物可追溯；修改策略生成新结果版本 |
| Chunk | chunk_id、适用的归属/版本、kind、parent_id、顺序、标题、text、source_refs；邻接可保存或派生 |
| text / search_text | 引用与展示文本、检索增强文本分开；证据不使用增强文本的偏移冒充原文位置 |
| source_refs | 允许多元素/多页来源；位置不可用时明确为空，保留精度 |
| ExtractionResult | 业务 data 遵守输入 Schema，证据与元信息独立；字符索引单位与文本摘要明确 |
| Agent 公共事件 | 类型、稳定事件/运行/调用关联、语义与版本；暂停、完成和工具结果不混淆 |
| StateEnvelope | 框架、格式版本与配置引用明确；原生负载不作为跨框架自动恢复保证 |

独立解析/抽取可使用调用方标识或组件生成的输入标识，不要求必须拥有 kb_id 或会话 ID；进入知识库后再建立归属关系。

### 10.2 Markdown 文档清单

| 文档 | 内容 |
| --- | --- |
| README.md / COMPONENTS.md | 组件清单、独立输入输出、最小依赖、可选集成与交付状态 |
| PARSING.md / CHUNKING.md | 格式矩阵、产物、策略单位、章节/表格、父子和来源 |
| KNOWLEDGE_BASE.md / RETRIEVAL.md | 版本、任务、索引发布、删除、召回/重排/扩展、分数与结果 |
| MODEL_CALLING.md | 供应商、别名、规则、能力、调用、重试回退和日志 |
| EXTRACTION_COMPONENT.md | Chunk/Document＋Schema 契约、策略、证据、结果、错误和范围 |
| EXTRACTION_ADAPTERS.md | LangExtract Provider、可选 Docling 来源、Agent 工具/HTTP/任务适配 |
| AGENT_ADAPTER.md | 公共契约、原生映射、实例生命周期、能力差异、状态与迁移 |
| EXECUTION_CONTROL.md / TOOLS.md | 执行策略、工具定义、校验、身份、SDK/API/MCP 接入 |
| STATE_AND_RECOVERY.md | 保存/加载、恢复判定、执行记录、版本、持久化与恢复演练 |
| CONTEXT_COMPONENT.md | 原生上下文配置、压缩、卸载、快照和计数语义 |
| SKILLS_COMPONENT.md | 格式、加载、版本、资源、注册/挂载与错误 |
| APPROVAL_COMPONENT.md | 原生权限、公共请求/决定、绑定、身份、存储、事件和显式恢复 |
| EVALUATOR_COMPONENT.md / EVALUATION.md | 样本、标注、指标、阈值、报告、误差与对比结果 |
| EXTENDING.md / OPERATIONS.md | Provider/Adapter 扩展、版本、容量、备份、清理与部署 |

文档先说明单组件接口，再说明可选组合；所有示例必须匹配真实实现，不将完整业务系统作为调用基础组件的前提。

### 10.3 实施前仍需确定或验收

- 第 6—8 点修订方案的最终确认，以及后续未选型方向的优先级。
- 组件包/版本、首期文件格式、目标 Schema 支持范围及目标供应商能力。
- LLM、Embedding、重排与裁判模型的实际 Provider 配置；具体模型不在此文指定。
- 中文分词、分块参数、检索/重排预算、超时/并发、日志与数据保留配置。
- 真实样本、标注、验收阈值、容量与故障恢复测试。

AgentScope 原生接口的已有核对以 v2.0.11 资料为基线，不等于项目已完成锁版。Docling 可在 CPU 使用；离线运行需提前准备所需依赖和模型资产。LLM/Embedding/重排是否离线取决于实际 Provider，不由本地存储方案自动保证。

验收分别覆盖解析/分块来源、检索 Recall/nDCG、字段 precision/recall、Schema 合规、证据准确性、运行/工具状态恢复、权限/审批对应关系、延迟和资源。当前尚未给出未经实测的性能或准确率结论。
