# 文档分块组件：实现与接入

本轮已实现此前列为“未实现／部分实现／未封装”的分块相关功能。业务方独立调用组件，流程由使用方决定。关键代码均带中文多行 docstring，并对偏移转换、表头来源、窗口接缝、版本隔离及 MMR 选择补充中文解释。

- [功能—代码—测试逐项核对](reviews/CHUNKING_REVIEW.md)
- [逐文件代码 Markdown](code/CHUNKING_CODE_REVIEW.md)
- [本地资源与并发记录](benchmarks/chunking-local.json)

## 1. 原功能清单逐项落实

| 功能 | 当前状态 | 可调用入口 / 实现方式 |
| --- | --- | --- |
| 固定字符切分 | 已实现 | `Chunker.chunk`，`strategy="character"` |
| 窗口重叠 | 已实现 | 六种策略都可配置 `overlap`；普通流式文本窗口接缝也补上下文 |
| 递归规则切分 | 已实现 | `strategy="recursive"`，原生 Chonkie RecursiveChunker |
| 按分隔符切分 | 已实现 | `strategy="delimiter"`，字面分隔符可配置，支持归前块／后块 |
| 父子块构建 | 已实现 | `ParentChildBuilder.build`，显式范围，完整预检及全局偏移 |
| 自动按章节构造父块 | 已实现 | `SectionBuilder.build`、`ParentChildBuilder.build_sections`，读取 Docling 原生标题层级 |
| Token 分块 | 已实现 | `strategy="token"`，注入实际编码器，按真实 Token 预算在原文字符边界切分 |
| 独立句子策略 | 已实现 | `strategy="sentence"`，Chonkie 句末边界；配置每片句数及硬大小上限 |
| 语义模型分块 | 已实现 | `strategy="semantic"`，原生 Chonkie SemanticChunker＋显式向量提供方 |
| 表格保持与超长表格切分 | 已实现 | 默认保留表格；按完整数据行切分，为后续片重复表头并记录表头来源 |
| 父块补充 | 已实现 | `ContextExpander.expand(parent_mode="append"/"replace")` |
| 相邻块扩展 | 已实现 | `ContextExpander.expand(before=…, after=…)`，限制版本和父范围，遇到缺失序号停止 |
| MMR | 已实现 | `mmr_select`，查询相关性与已选内容冗余的贪心选择 |
| 精确／近重复去重 | 已实现 | `deduplicate`，NFKC、大小写／空白规范化与字符 shingles Jaccard |
| 原文件 SourceMap | 已实现 | `DoclingParser.from_native/parse` 建立映射，`SourceMapper.resolve` 查询元素、页码和矩形 |
| 大文本流式分片 | 已实现 | `StreamingChunker.iter_text/iter_file`，磁盘快照＋有界窗口，支持六种策略和跨窗口表格 |
| 解析分批消费 | 已实现 | `DoclingParser.iter_parse` 逐文件；`iter_pdf` 使用原生 PDF 页范围分批转换 |
| 资源与并发验证 | 已执行 | 大文本完整覆盖、资源上限、八线程确定性；详细数值与命令见第 9 节 |

“已实现”指组件及其对应检查已落地，不表示任意模型、所有业务数据或任意硬件已达到生产质量指标。模型／权重由使用方配置；PDF 实际权重推理的验收范围见第 9 节。

## 2. 模块与职责

| 模块 | 职责 | 公开入口 |
| --- | --- | --- |
| [chunking/chunker.py](../src/agent_components/chunking/chunker.py) | 策略配置、分片身份、父子关系、章节组合 | ChunkPolicy、Chunker、ParentChildBuilder |
| [chunking/backends.py](../src/agent_components/chunking/backends.py) | 无损 Token、Chonkie 句子／语义能力 | TokenEncoder、EmbeddingProvider |
| [chunking/sections.py](../src/agent_components/chunking/sections.py) | 原生标题层级到不重叠章节范围 | SectionBuilder、Section |
| [chunking/tables.py](../src/agent_components/chunking/tables.py) | 按完整数据行切分表格，保留表头来源 | split_table |
| [chunking/sources.py](../src/agent_components/chunking/sources.py) | 规范文本到原生元素来源查询 | SourceMapper |
| [parsing/docling.py](../src/agent_components/parsing/docling.py) | Docling 解析与结构化序列化 | DoclingParser |
| [chunking/streaming.py](../src/agent_components/chunking/streaming.py) | 文本流快照、窗口化分析、表格行流 | StreamingChunker |
| [core/markdown_tables.py](../src/agent_components/core/markdown_tables.py) | 解析和分块共用的 GFM 表格识别 | markdown_table_ranges |
| [context.py](../src/agent_components/context.py) | 父／邻块补充、重叠正文合并和总预算 | ContextExpander、ContextResult |
| [selection.py](../src/agent_components/selection.py) | MMR、文本去重及去重依据 | RetrievalCandidate、mmr_select、deduplicate |
| [contracts.py](../src/agent_components/core/contracts.py) | 数据与位置契约 | Document、Chunk、ChunkResult、DocumentElement、SourceSpan、TableSlice |

这些入口不要求 Agent、数据库或固定业务 DAG。需要真实 Token 或语义能力时显式注入对应提供方；不隐式下载模型，也不引入 LangChain。

## 3. 输入输出契约

### 3.1 Document → ChunkResult

`Chunker(...).chunk(document, policy)` 返回 ChunkResult；`ParentChildBuilder(chunker).build(...)` 和 `build_sections(...)` 返回同一类型。

| 字段 | 精确含义 |
| --- | --- |
| `Document.text` | 唯一规范原文，分块不自动清洗、改换行或归一化 Unicode |
| `Document.elements` | Docling 原生标题、表格和文本的有序范围；标题 level 来自原生结构 |
| `Document.source_map` | 规范文本范围 → 原生元素引用，可带页码和矩形 |
| `Chunk.text / start / end` | 始终满足 `text == document.text[start:end]`，Python 字符左闭右开范围 |
| `Chunk.context_prefix` | 后续表格分片重复的表头，单独保存；不计作正文的新原文范围 |
| `Chunk.index_text` | 只读属性：`context_prefix + text`，索引与模型接入应按需使用；JSON 序列化默认不包含该属性 |
| `Chunk.table` | 表格身份、序列化后数据行 `[row_start,row_end)`、原文表头范围及 oversized 标志 |
| `Chunk.ordinal` | parent／leaf 各自独立的文档级零起点序号 |
| `Chunk.parent_id / ordinal_in_parent` | 父身份和父范围内的子序号 |
| `Chunk.section_path` | 自动章节模式的标题祖先路径 |
| `Chunk.source_refs` | 精确规范文本引用，以及 SourceMap 能提供的原生元素来源 |
| `ChunkResult.parents / leaves` | 只读过滤属性；通常只把 leaves 写入叶子索引 |
| `input_length / covered_ranges` | 完整原文长度与实际处理范围，不把父范围间隙伪报为全文 |
| `config_fingerprint / warnings` | 配置身份和来源缺失／显式表格超限提示 |

`Chunk.text` 的偏移不是 Token 下标、文件字节下标或视觉字形下标。Emoji 序列和组合字符可能包含多个 Python 字符。Token 策略不会通过解码半个 Token 生成替换字符；它始终选择原文字符边界。

### 3.2 ChunkPolicy 参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `strategy` | recursive | character／recursive／delimiter／token／sentence／semantic |
| `size` | 1000 | token 策略为真实 Token 上限；其他策略为字符上限 |
| `overlap` | 0 | 字符策略为精确重复字符；其他策略最多补充的前文量；token 按实际 Token 计 |
| `delimiters` | `("\n\n", "\n")` | delimiter 字面分隔符序列，可包含一个或多个字符串 |
| `include_delimiter` | previous | 分隔符归前片／后片，始终保留 |
| `recursive_delimiters` | 段落／换行 → 中英文句末 | 可配置完整优先级层级，之后为空白、字符回退 |
| `sentence_delimiters` | 中英文句末及换行 | sentence 和 semantic 的原生句末规则 |
| `sentences_per_chunk` | 1 | sentence 每片最多句子数，超长单句仍受 size 限制 |
| `semantic_threshold` | 0.8 | Chonkie 原生峰值筛选参数，不等于“相邻余弦低于此值就切” |
| `similarity_window` | 3 | 原生前文比较的句子窗口 |
| `embedding_batch_size` | 32 | 单次提供方请求的最大文本条数 |
| `semantic_window_chars` | 32000 | 单次语义分析窗口上限，限制完整句子组的分析范围 |
| `preserve_tables` | True | 按完整行保留表格；表格不执行普通窗口重叠 |
| `table_overflow` | error | 单行加表头无法容纳时报错；allow 显式允许并返回 oversized／warning |

size、overlap 等整数参数拒绝 bool、字符串和浮点强制转换；`0 <= overlap < size`。分隔符必须非空且单层不重复。构造配置时和组件调用入口都会校验，model_copy／model_construct 不能绕过入口检查。

## 4. 六种策略的实际逻辑

1. **字符**：固定原文窗口，步长为 `size-overlap`；到达末尾即停止。
2. **递归**：调用 Chonkie RecursiveChunker，使用可配置分隔层级和 character tokenizer；验证每个原生输出的端点、正文、大小和完整覆盖。启用重叠时先预留重叠预算再补前文。
3. **分隔符**：对用户字面量进行正则转义，优先匹配更长分隔符；保留分隔符归属；超长段继续按字符预算拆分。
4. **Token**：用提供方 `encode(text)` 的实际长度计量，在原文字符边界探测合法片段并复验预算。BPE 计数不严格单调，因此只保证无损覆盖和预算，不承诺得到数学上最长的每个窗口；预算无法容纳当前字符时明确报错。
5. **句子**：使用 Chonkie 的句末切分能力，按 `sentences_per_chunk` 组合；保留原文及空白，超长单句继续分割。
6. **语义**：向量提供方桥接原生 Chonkie BaseEmbeddings，调用 SemanticChunker；采用原生平滑与峰值检测，关闭非连续 skip merge，保持原文顺序。验证提供方返回的数量、维度、有限性和非零性；按批调用、窗口分析，最终复验原文并执行字符硬上限。

`Chunker(tokenizer=..., tokenizer_id=...)` 接受提供 `encode(str) -> Sequence[int]` 的已加载编码器。配置须关闭自动 BOS／EOS／padding，身份应包含词表及预处理版本。

`Chunker(embedding=..., embedding_id=...)` 接受有固定 `dimension` 和 `embed_batch(texts)` 的提供方。身份应包含模型及预处理版本。提供方可使用本地 CPU 模型或既有远程服务，组件不选择供应商。

仅 character／delimiter／token 不需要 Chonkie；recursive／sentence／semantic 需要 `chunking` extra。空递归输入不导入可选框架。

## 5. 章节、表格与 SourceMap

### 5.1 章节父子块

`SectionBuilder.build(document, max_level=6, include_preamble=True)` 从 `Document.elements` 的 heading 读取 level；祖先标题存为 path。纳入的标题边界被平铺成不重叠父范围，未纳入的更深标题留在上级章节正文中。

`ParentChildBuilder.build_sections` 直接组合这些范围和子策略。没有结构标题时全文视为一个分节，空文本为空结果；不会把正文中的井号当标题。需要自动章节时应传入 Docling 的 Document，或由调用方显式提供可信 DocumentElement。

显式 `build` 允许有间隙或空父范围，但先完整校验全部区间。保留表格时，任何父范围切穿表格都在子块工作前报错。每个父块内部独立切分，局部位置加父起点一次，输出全局位置与父内顺序。

### 5.2 表格

优先使用原生 table 元素；纯 Markdown 输入可识别 GFM 表格。代码围栏内的表格文本不按表格处理。表格若能放入预算则整体保留；过长时逐个完整数据行装箱，预算包含重复表头。

后续片 `text` 只包含原文数据行，表头存于 `context_prefix`，位置存于 `table.header_start/header_end`。`row_start/row_end` 指 Markdown 序列化后的数据行，不冒充原始合并单元格网格坐标。原始 TableData、合并单元格结构仍在 `ParsedDocument.native_json`；Docling 的 GFM 输出可能将多行表头展平。

无法按 GFM 表示的原生表格作为原子元素处理。对无法容纳的一整行／表头，error 明确失败，allow 保留内容并标记 oversized；不存在同时满足“不可拆行”和“任意小硬预算”的通用切法。

### 5.3 SourceMap

`DoclingParser.from_native` 按原生 DocItem 阅读顺序逐个序列化并累计位置；不对完整 Markdown 执行首次 find。DOCX 可能用 Group 包住整章，因此不能把顶层 Group 的合并输出误当成单个标题。

SourceMap 记录每个已渲染部件的原文范围与原生元素；`SourceMapper` 通过二分查找起始候选并读取相交范围。Chunk 始终携带规范文本引用，另按相交区间附原生 element_id、page、bbox、coord_origin。

**精度是规范文本字符精确＋原文件元素级。** 一个部件含多个原生元素时会保留多个引用；元素可能有多页 provenance。组件不线性缩放 bbox，不编造原文件字符坐标。生成的分隔空白可能只有规范文本引用；TXT 没有页码，缺失的信息保持缺失。无 SourceMap 的旧式 source_refs 会产生 `source_mapping_unavailable` 提示。

## 6. 检索后的独立组件

### 6.1 父块／相邻扩展

`ContextExpander.expand(hits, corpus, before=1, after=1, parent_mode="none", stay_in_parent=True, max_characters=8000)` 接收已经由使用方完成权限和发布版本过滤的语料。

- 命中必须与语料中同 ID 的完整块一致，避免使用外部伪造命中。
- 相邻块通过 `(document_id, revision_id, ordinal)` 定位；默认不跨父块，遇到缺失序号立即停止，不跳过不可见内容寻找更远块。
- append 补充父块，replace 使用父块替换子块；父块必须可见，且文档版本和包含关系一致。
- 合并连续或重叠区间，先核对重叠正文，再去掉重复字符；原文空隙不会被猜测补齐。
- 总预算包含窗口的重复表头；不截断正文，超预算块身份记录在 omitted_ids。

### 6.2 MMR 和近重复

`mmr_select(candidates, query_vector, limit=10, relevance_weight=0.5, max_candidates=2000)` 要求候选携带实际向量。第一项按查询相关性，之后按 `λ*cos(query,item) - (1-λ)*max_cos(item,selected)` 选择；同分保留输入次序。候选 relevance 保留原始召回信息，算法不把未归一化 BM25 分数与余弦混用。

`deduplicate(candidates, threshold=0.85, shingle_size=3, max_candidates=2000)` 按输入排序保留首次出现的文本。先执行 NFKC、casefold 和空白规范化；精确相同直接去重，其余用字符 n-gram 集合的 Jaccard 过滤。返回 duplicate_of、similarity、reason。该算法提供文本近重复识别，不保证发现改写后的语义重复。

顺序、候选数量、阈值和是否执行这些组件均由使用方决定，组件不固定一套 RAG 流程。

## 7. 大文档与流式边界

`StreamingChunker(chunker, window_chars=65536, max_input_chars=100_000_000, max_chunks=1_000_000)` 提供：

- `iter_text(pieces, policy, document_id=..., revision_id=None)`：接受任意传输边界的字符串流。
- `iter_file(path, policy, document_id=..., revision_id=None)`：UTF-8／BOM 文本读取，保留 CRLF；检测快照期间文件签名变化。

先用临时文件生成不可变规范文本快照及完整摘要，再进行窗口分析，不保留全文 Python 字符串。输入字符数和产物数有硬上限，关闭迭代器会清理临时文件。没有显式 revision 时使用规范文本摘要。

普通文本按窗口调用所选六种策略。字符重叠在窗口接缝继续保留；其他策略按计量单位最多补前文。表格逐行读取并跨窗口保持表头，不在普通分析窗口处切断整行。单个表格行超过读取窗口会明确报错，应提高窗口或使用内存接口处理该行。窗口影响上下文分组，因而进入配置摘要；流式结果不承诺与整篇分析得到完全相同边界。

`DoclingParser.iter_parse(paths)` 逐文件产出。`iter_pdf(path, pages_per_batch=10)` 通过 Docling 原生 `page_range` 分批转换；产物身份为 `原文档ID#pages=起-止`，metadata 保留 source_document_id、page_start、page_end。每批的规范文本坐标独立，原生 provenance 仍保留页码；不伪装为已拼接的整篇全局 ordinal，也不自动合并跨批章节／表格。

PDF 单批解析仍需要离线权重或显式允许下载，原生引擎可能缓存模型和页面元信息。Office 转换按 Docling 原生单文件方式运行，调用方可逐文件消费并对转换后的规范文本流式分片。这里不声称任意格式都能在任意固定 RSS 下完成解析。

迭代器可能在产出后遇到后续错误；调用方应在整批成功后发布索引产物。组件不自行建立业务队列或发布流程。

## 8. ID、错误和兼容性

块 ID 包含：文档 ID、revision、完整规范文本 SHA-256、绝对区间、kind、配置摘要、parent_id、重复前缀和表格元数据。

配置摘要纳入 `chunking-v3`、完整策略参数、父范围和真实后端身份；流式模式还纳入 stream-v1 和分析窗口。相同输入和配置可重现；正文变化会改变整批身份，即使调用方忘记更新 revision。依赖版本仍由 uv.lock 固定。

**本轮 ID 和解析结构发生变化，已有分片必须重建。** 新增 Document.elements/source_map、Chunk.section_path/context_prefix/table；ChunkResult 维持 input_length、covered_ranges 等字段。只应发布一次执行的一致产物，不混用新旧分块配置。

| 情况 | 返回／异常 |
| --- | --- |
| 配置、范围、提供方、维度或资源参数无效 | ValueError；配置构造阶段为 Pydantic ValidationError |
| 缺少所选可选依赖 | MissingDependencyError，给出 extras |
| Chonkie 无法完整保留原文或位置非法 | ChunkAlignmentError，不模糊匹配、不静默替换策略 |
| 表格单行超预算且选择 error | ValueError |
| 显式 allow 表格超限 | Chunk.table.oversized；内存结果附 oversized_table_row 提示 |
| 上游引用无 SourceMap | 规范文本来源＋source_mapping_unavailable |
| 相邻语料缺口 | 停止该方向扩展 |
| 指定父块不可见／关系错误 | ValueError |
| 上下文总预算不足 | 不裁正文，记录 omitted_ids |

## 9. 验证与复现

真实执行范围包括：Chonkie 递归／句子／语义、离线训练的真实 BPE 编码器、Docling Markdown／HTML／DOCX、原生重复元素及页码矩形、PDF 页范围调度、流式文本与跨窗口表格、MMR、近重复、可见性缺口、预算和线程确定性。

PDF 页范围用真实五页 PDF 的页面计数、原生 DoclingDocument 和转换器替身验证 `(1,2)/(3,4)/(5,5)` 调度及分段身份；**未配置 PDF 模型权重，不把这项检查当成真实 PDF 推理准确率或内存验收**。语义测试使用已知向量提供方验证真实原生算法，不宣称任何具体线上模型的业务准确率。

本地资源检查：25,165,824 个规范文本字符，71,303,169 个 UTF-8 字节，25,344 个块，完整覆盖；启用 tracemalloc 的耗时约 14.65 秒，跟踪 Python 内存峰值 2,191,924 字节。八线程、16 次、每次 1,000,000 字符约 1.57 秒，全部 ID 摘要一致。整个进程峰值 RSS 38,900 KiB。数字是本环境记录，不是生产 SLA；源码和原始 JSON 可复核。

```sh
uv sync --locked --all-extras
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests migrations scripts
uv run --no-sync ruff format --check src tests migrations scripts
uv run --no-sync mypy src
uv run --no-sync python scripts/benchmark_chunking.py --output docs/benchmarks/chunking-local.json
```

分项测试入口与功能关系见检查记录；全量结果见 [VALIDATION.md](VALIDATION.md)。
