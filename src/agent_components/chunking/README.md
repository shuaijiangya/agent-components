# 文档分块组件

`agent_components.chunking` 接收共享契约 `Document`，输出带原文范围、文档版本和来源引用的块。纯文本可直接构造 `Document`，Word 等文件可先通过[解析组件](../parsing/README.md)转成同一结构。

## 业务逻辑

1. 校验 `ChunkPolicy`，再按策略处理正文。所有偏移使用 `Document.text` 的字符坐标，满足 `chunk.text == document.text[chunk.start:chunk.end]`。
2. 默认保留表格。识别到表格后，表格和前后正文独立切分。超长表格按完整数据行切分，后续表格分片把表头放在 `context_prefix`。向量索引和模型输入可读 `index_text`，直接取得表头和数据行。表头加一行超过预算时，默认报错，可用 `table_overflow="allow"` 保留超长行并返回提示。
3. 父子分块先确定父块范围，再在每个父块内按子块策略切分。`build()` 接收外部提供的范围，`build_sections()` 使用结构化标题。子块通过 `parent_id` 关联父块，自动章节模式还会保留 `section_path`。
4. 大文本可用 `StreamingChunker` 做磁盘快照并逐块迭代。输入为 UTF-8 文件或文本片段，输出块带全局字符偏移。

## 使用说明

| 策略 | 切分依据 | 依赖或输入 |
| --- | --- | --- |
| `character` | 固定字符窗口，可配置重叠 | 核心依赖 |
| `recursive`（默认） | 逐级使用段落、换行和标点边界 | `chunking` 依赖组 |
| `delimiter` | 一个或多个字面分隔符 | 核心依赖 |
| `token` | 分词器计算的实际 Token 预算 | 注入 `tokenizer` 和 `tokenizer_id` |
| `sentence` | 句子边界和每块最大句数 | `chunking` 依赖组 |
| `semantic` | 句子向量的语义边界 | `chunking` 依赖组及 `embedding`、`embedding_id` |

`size` 对 `token` 使用实际 Token 数，对其他策略使用字符数。`overlap` 必须小于 `size`。更多配置与边界见[分块契约与策略说明](../../../docs/CHUNKING.md)。

以下 Python 代码块可分别保存为仓库根目录下的 `example.py`，通过 `uv run --no-sync python example.py` 运行。核心依赖可运行字符、分隔符、手工章节和这些策略的流式示例。`chunking` 提供 Chonkie，`parsing` 提供 Word 解析和示例使用的 `tokenizers`，`agent` 提供连接真实 Embedding 服务所需的 OpenAI 客户端。

```sh
uv sync --locked --extra chunking --extra parsing --extra agent
```

如果使用已激活的 Conda 环境，从仓库根目录运行 `UV_PROJECT_ENVIRONMENT="$CONDA_PREFIX" uv sync --locked --inexact --extra chunking --extra parsing --extra agent`。

| 调用入口 | 可复现示例 |
| --- | --- |
| `Chunker.chunk()` | [字符](#固定字符分块)、[递归](#递归分块)、[分隔符](#字面分隔符分块)、[Token](#按-token-预算分块)、[句子](#句子分块)、[语义](#语义分块) |
| `ChunkPolicy` / `ChunkResult` | [配置和结果序列化](#配置结果与稳定身份)、[表格预算](#表格分片与溢出) |
| `ParentChildBuilder.build()` | [业务范围和部分覆盖](#手动指定父块范围) |
| `ParentChildBuilder.build_sections()` / `SectionBuilder.build()` | [Word 章节](#word-父子分块示例)、[业务标题与序言](#显式标题与序言) |
| `SourceMapper.resolve()` | [元素级来源和仅文本回退](#来源定位) |
| `StreamingChunker.iter_file()` / `iter_text()` | [文件和分段输入](#流式分块示例)、[流式表格和资源限制](#跨窗口表格与资源上限) |

## 固定字符分块

```python
from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import Document

document = Document(id="character-demo", text="甲乙丙丁戊己庚辛")
policy = ChunkPolicy(strategy="character", size=4, overlap=2)
chunker = Chunker()
result = chunker.chunk(document, policy)

for chunk in result.leaves:
    print(chunk.start, chunk.end, chunk.text)
    assert chunk.text == document.text[chunk.start:chunk.end]

assert result.config_fingerprint == chunker.configuration_fingerprint(policy)
```

输出区间 `[0, 4)`、`[2, 6)`、`[4, 8)`。配置摘要可写入入库记录，用于区分切分规则。

## 递归分块

`recursive_delimiters` 的外层是优先级，内层是同一优先级的分隔符。下例优先按 `@@` 切，再按句末标点切，超出预算时继续细分。

```python
from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import Document

document = Document(id="recursive-demo", text="合同审核@@付款审批。财务复核。出纳确认@@供应商交付")
policy = ChunkPolicy(
    strategy="recursive",
    size=12,
    recursive_delimiters=(("@@",), ("。", "！", "？")),
)
result = Chunker().chunk(document, policy)

print([chunk.text for chunk in result.leaves])
assert "".join(chunk.text for chunk in result.leaves) == document.text
```

## 字面分隔符分块

```python
from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import Document

document = Document(id="delimiter-demo", text="提交申请。负责人复核||财务付款")
chunker = Chunker()
single = ChunkPolicy(strategy="delimiter", delimiters=("||",), size=80)
multiple = ChunkPolicy(
    strategy="delimiter",
    delimiters=("。", "||"),
    size=80,
    include_delimiter="next",
)

for policy, expected in (
    (single, ["提交申请。负责人复核||", "财务付款"]),
    (multiple, ["提交申请", "。负责人复核", "||财务付款"]),
):
    result = chunker.chunk(document, policy)
    assert [chunk.text for chunk in result.leaves] == expected
    print(expected)
```

JSON 配置通过数组传一个或多个分隔符，例如 `"delimiters": ["||"]`。每个字符串代表一个完整的字面分隔符。`include_delimiter` 默认为 `previous`，示例中的 `next` 将分隔符保留在后块。

## 按 Token 预算分块

下例在内存里训练一个小型 BPE 分词器，便于本地验证实际 Token 计数。业务接入时可用[本地分词器工厂](../../../examples/providers.py)加载目标模型对应的 `tokenizer.json`。

```python
from tokenizers import Tokenizer
from tokenizers.models import BPE
from tokenizers.pre_tokenizers import ByteLevel
from tokenizers.trainers import BpeTrainer

from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import Document

text = "支付金额为3200元。\n仓库负责验收入库。🙂"
tokenizer = Tokenizer(BPE(unk_token="[UNK]"))
tokenizer.pre_tokenizer = ByteLevel(add_prefix_space=False)
trainer = BpeTrainer(
    vocab_size=300,
    special_tokens=["[UNK]"],
    initial_alphabet=ByteLevel.alphabet(),
)
tokenizer.train_from_iterator([text] * 5, trainer)


class Encoder:
    def encode(self, value: str) -> list[int]:
        return tokenizer.encode(value, add_special_tokens=False).ids


encoder = Encoder()
document = Document(id="token-demo", text=f"{text}\n" * 4)
policy = ChunkPolicy(strategy="token", size=12)
result = Chunker(tokenizer=encoder, tokenizer_id="sample-bpe-v1").chunk(document, policy)

assert len(result.leaves) > 1
assert "".join(chunk.text for chunk in result.leaves) == document.text
for chunk in result.leaves:
    count = len(encoder.encode(chunk.index_text))
    assert count <= policy.size
    print(count, repr(chunk.text))
```

`tokenizer_id` 应体现词表、模型和预处理版本。模型提示和特殊 Token 的预算由调用方预留。

### 加载真实模型的本地分词器

设置 `TOKENIZER_PATH` 指向目标模型的 `tokenizer.json`，将模型版本和编码配置写入 `TOKENIZER_ID`。工厂读取本地文件，关闭自动添加特殊 Token。

```python
from os import environ
from pathlib import Path

from agent_components.chunking import Chunker, ChunkPolicy, TokenEncoder
from agent_components.parsing import DoclingParser
from examples.providers import local_tokenizer

encoder: TokenEncoder = local_tokenizer()
document = (
    DoclingParser().parse(Path("examples/data/contract.txt"), document_id="contract-001").document
)
policy = ChunkPolicy(strategy="token", size=256, overlap=24)
result = Chunker(tokenizer=encoder, tokenizer_id=environ["TOKENIZER_ID"]).chunk(document, policy)

for chunk in result.leaves:
    assert chunk.text == document.text[chunk.start : chunk.end]
    assert len(encoder.encode(chunk.index_text)) <= policy.size
    print(chunk.id, chunk.start, chunk.end, len(encoder.encode(chunk.index_text)))
```

## 句子分块

```python
from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import Document

document = Document(id="sentence-demo", text="第一句。第二句！第三句？第四句。")
policy = ChunkPolicy(
    strategy="sentence",
    size=60,
    sentences_per_chunk=2,
    sentence_delimiters=("。", "！", "？"),
)
result = Chunker().chunk(document, policy)

assert [chunk.text for chunk in result.leaves] == ["第一句。第二句！", "第三句？第四句。"]
print([chunk.text for chunk in result.leaves])
```

`sentences_per_chunk` 控制每块最多容纳的句数，仍需满足 `size` 的字符预算。

## 语义分块

下例用主题词计数构造可复现的测试向量，演示 Chonkie 如何使用注入的向量提供方。业务接入可用同一 `embed_batch` 接口连接[兼容的向量端点](../../../docs/examples/chunking.md#语义分块接入真实-embedding)。

```python
from collections.abc import Sequence

from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import Document


class TopicEmbeddings:
    dimension = 3

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [
            [
                1.0 + sum(text.count(word) for word in ("付款", "金额", "发票")),
                1.0 + sum(text.count(word) for word in ("库存", "仓库", "物流")),
                1.0,
            ]
            for text in texts
        ]


payments = "付款金额与发票。" * 10
warehouse = "仓库库存和物流。" * 10
document = Document(id="semantic-demo", text=payments + warehouse)
policy = ChunkPolicy(
    strategy="semantic",
    size=128,
    similarity_window=1,
    embedding_batch_size=2,
    semantic_window_chars=1000,
)
result = Chunker(embedding=TopicEmbeddings(), embedding_id="topic-demo-v1").chunk(document, policy)

assert [chunk.text for chunk in result.leaves] == [payments, warehouse]
print([(chunk.start, chunk.end) for chunk in result.leaves])
```

### 接入真实 Embedding 服务

由运行环境提供 `EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`EMBEDDING_DIMENSION`、`EMBEDDING_API_KEY` 和版本化的 `EMBEDDING_ID`。模型与维度应与端点配置一致。组件按 `embedding_batch_size` 限制单次 Provider 输入数量，并检查返回数量、维度、有限值和非零向量。

```python
from contextlib import closing
from os import environ
from pathlib import Path

from agent_components.chunking import Chunker, ChunkPolicy, EmbeddingProvider
from agent_components.parsing import DoclingParser
from examples.providers import compatible_embeddings

document = (
    DoclingParser().parse(Path("examples/data/contract.txt"), document_id="contract-001").document
)
policy = ChunkPolicy(
    strategy="semantic",
    size=160,
    semantic_threshold=0.8,
    similarity_window=2,
    embedding_batch_size=16,
    semantic_window_chars=4096,
)
with closing(compatible_embeddings()) as http_provider:
    provider: EmbeddingProvider = http_provider
    chunker = Chunker(embedding=provider, embedding_id=environ["EMBEDDING_ID"])
    result = chunker.chunk(document, policy)

assert "".join(chunk.text for chunk in result.leaves) == document.text
print(result.config_fingerprint, len(result.leaves))
```

`size` 以字符计。`semantic_window_chars` 限制送入一次原生语义分析的字符数，边界会受分析窗口影响。语义分块返回文本块，索引向量仍由调用方对最终 `index_text` 计算。

## 配置、结果与稳定身份

将策略存成 JSON 后用 `ChunkPolicy` 校验，使用 `ChunkResult` 恢复完整结果。下面验证重跑身份、完整覆盖、空文档和原文坐标。

```python
from pathlib import Path

from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import ChunkResult, Document

policy_path = Path("examples/data/policy-character.json")
policy = ChunkPolicy.model_validate_json(policy_path.read_text(encoding="utf-8"))
document = Document(id="contract-001", revision_id="contract-version-3", text="验收后30日内支付。")
chunker = Chunker()
result = chunker.chunk(document, policy)
restored = ChunkResult.model_validate_json(result.model_dump_json())

assert restored == result == chunker.chunk(document, policy)
assert restored.covered_ranges == ((0, len(document.text)),)
assert restored.input_length == len(document.text)
assert restored.config_fingerprint == chunker.configuration_fingerprint(policy)
assert all(chunk.text == document.text[chunk.start:chunk.end] for chunk in restored.leaves)
empty = chunker.chunk(Document(id="empty-contract", text=""), policy)
assert empty.chunks == empty.covered_ranges == ()
```

普通分块的 `covered_ranges` 表示全文处理范围。父子分块返回实际选用的父范围，可能有间隔。块身份由文档 ID、版本、内容、策略配置和注入能力版本共同确定。`index_text` 是属性，恢复 `Chunk` 后读取即可。

## 手动指定父块范围

已有业务章节边界时，每组 `[start, end)` 对应一个父块。子策略分别在每个父块内部生效。

```python
from agent_components.chunking import Chunker, ChunkPolicy, ParentChildBuilder
from agent_components.core.contracts import Document

document = Document(id="manual-demo", text="采购审批与签署\n内部批注\n到货核验与归档")
ranges = [(0, document.text.index("内部批注")), (document.text.index("到货"), len(document.text))]
policy = ChunkPolicy(strategy="character", size=4)
chunker = Chunker()
result = ParentChildBuilder(chunker).build(document, ranges, policy)
assert result.covered_ranges == tuple(ranges)
assert result.config_fingerprint == chunker.configuration_fingerprint(policy, ranges=ranges)
assert [child.ordinal for child in result.leaves] == list(range(len(result.leaves)))

parents = {parent.id: parent for parent in result.parents}
for parent in result.parents:
    children = [child for child in result.leaves if child.parent_id == parent.id]
    assert [child.ordinal_in_parent for child in children] == list(range(len(children)))
for child in result.leaves:
    parent = parents[child.parent_id]
    assert parent.start <= child.start < child.end <= parent.end
    print(parent.text, "→", child.text)
```

父块保留完整业务范围，`size` 作用于子块。父范围按原文顺序提供，范围之间可以有间隔。重叠、反向、越界或切穿受保护表格时会报错。父块和叶块各自计数 `ordinal`。

## 显式标题与序言

已有业务标题坐标时，可以直接传 `DocumentElement`。同一入口支持来自 Docling 的 `Document`。

```python
from agent_components.chunking import ChunkPolicy, ParentChildBuilder, Section, SectionBuilder
from agent_components.core.contracts import Document, DocumentElement

text = "合同说明\n第一章 交付\n交付清单\n第一节 验收\n验收期限为15个工作日。"
headings = []
for level, title in [(1, "第一章 交付"), (2, "第一节 验收")]:
    start = text.index(title)
    headings.append(
        DocumentElement(id=f"heading-{level}", kind="heading", start=start,
                        end=start + len(title), level=level, title=title)
    )
document = Document(id="contract-with-headings", text=text, elements=tuple(headings))
sections: tuple[Section, ...] = SectionBuilder().build(document, max_level=2)
assert [section.path for section in sections] == [(), ("第一章 交付",), ("第一章 交付", "第一节 验收")]

result = ParentChildBuilder().build_sections(
    document, ChunkPolicy(strategy="character", size=24), max_level=2, include_preamble=False,
)
assert len(result.parents) == 2
assert result.covered_ranges[0][0] == headings[0].start
assert result.parents[1].section_path == ("第一章 交付", "第一节 验收")
```

## Word 父子分块示例

此示例需要 `parsing` 依赖组，并在仓库根目录运行。`max_level=1` 以一级标题切父块，二级标题及其正文保留在所属章内。改为 `max_level=2` 时，二级标题也会开启新的父块。缺少结构化标题时，`build_sections()` 会把全文作为一个父块。

```python
from pathlib import Path

from agent_components.chunking import ChunkPolicy, ParentChildBuilder, SectionBuilder, SourceMapper
from agent_components.parsing import DoclingParser

parsed = DoclingParser().parse(Path("tests/fixtures/parent_child_sample.docx"))
document = parsed.document
policy = ChunkPolicy(strategy="character", size=175)
builder = ParentChildBuilder()
by_chapter = builder.build_sections(document, policy, max_level=1)
by_section = builder.build_sections(document, policy, max_level=2)

for result in (by_chapter, by_section):
    print(len(result.parents), len(result.leaves))

for section in SectionBuilder().build(document, max_level=2):
    print(section.path, section.start, section.end)

child = by_section.leaves[0]
for ref in SourceMapper(document).resolve(child.start, child.end):
    print(ref.precision, ref.element_id, ref.start, ref.end)
```

按章得到 3 个父块和 7 个子块，按节得到 7 个父块和 9 个子块。文档主标题也形成一个父块。`SectionBuilder` 可单独预览父块范围，`SourceMapper` 可查任意规范文本区间的来源。完整断点示例见 [Word 集成测试](../../../tests/test_word_parent_child.py)。

## 表格分片与溢出

```python
from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import Document

header = "| 材料 |\n| --- |\n"
row = "| 合同 |\n"
document = Document(id="table-demo", text=f"开头正文\n\n{header}{row}| 清单 |\n\n结尾正文")
chunker = Chunker()
result = chunker.chunk(document, ChunkPolicy(strategy="character", size=len(header + row)))

table_chunks = [chunk for chunk in result.leaves if chunk.table is not None]
assert len(table_chunks) == 2
first, second = table_chunks
assert (first.table.row_start, first.table.row_end) == (0, 1)
assert (second.table.row_start, second.table.row_end) == (1, 2)
assert document.text[second.table.header_start:second.table.header_end] == header
print(repr(table_chunks[1].text))
print(table_chunks[1].index_text)
assert table_chunks[1].context_prefix == header

try:
    chunker.chunk(document, ChunkPolicy(strategy="character", size=len(header)))
except ValueError as exc:
    assert "table" in str(exc)
else:
    raise AssertionError("默认预算应拒绝表头加数据行超限")

plain = chunker.chunk(
    document, ChunkPolicy(strategy="character", size=len(document.text), preserve_tables=False)
)
assert len(plain.leaves) == 1 and plain.leaves[0].text == document.text

over_budget = chunker.chunk(
    document, ChunkPolicy(strategy="character", size=len(header), table_overflow="allow")
)
assert any(warning.code == "oversized_table_row" for warning in over_budget.warnings)
```

默认的 `table_overflow="error"` 会在表头加一行超过预算时抛出 `ValueError`。`allow` 会保留那一行并返回提示。

### 原生表格与代码围栏

如果 `Document.elements` 提供了表格元素，优先用这些范围。未经结构解析的 Markdown 可以从合法 GFM 表格回退识别，代码围栏里的表格写法仍按正文切分。Docling 提供的非 GFM 表格以整个元素为预算单位。

```python
from pathlib import Path

from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import Document
from agent_components.parsing import DoclingParser

parser = DoclingParser()
document = parser.parse(Path("examples/data/contract.md")).document
table_ids = {element.id for element in document.elements if element.kind == "table"}
assert table_ids
policy = ChunkPolicy(strategy="character", size=160)
chunks = Chunker().chunk(document, policy).leaves
assert {chunk.table.table_id for chunk in chunks if chunk.table} == table_ids

literal = Document(id="table-in-code", text="```markdown\n| 项目 |\n| --- |\n| 合同 |\n```\n")
assert all(chunk.table is None for chunk in Chunker().chunk(literal, policy).leaves)
```

## 来源定位

首条引用定位规范文本，后续引用标注相交的原生元素。页码和 `bbox` 只有后端提供时才有值。若旧数据只携带文档级 `source_refs`，分块返回精确文本范围和 `source_mapping_unavailable` 提示。

```python
from agent_components.chunking import Chunker, ChunkPolicy, SourceMapper
from agent_components.core.contracts import Document, SourceRef, SourceSpan

page_ref = SourceRef(input_id="contract-001", element_id="#/texts/0", page=2,
                     precision="element", bbox=(10, 20, 100, 40), coord_origin="TOPLEFT")
document = Document(id="contract-001", text="尾款验收后支付。",
                    source_map=(SourceSpan(start=0, end=8, refs=(page_ref,)),))
text_ref, element_ref = SourceMapper(document).resolve(2, 5)
assert (text_ref.precision, text_ref.start, text_ref.end) == ("text", 2, 5)
assert (element_ref.precision, element_ref.page, element_ref.bbox) == ("element", 2, page_ref.bbox)

legacy = Document(id=document.id, text=document.text, source_refs=(page_ref,))
result = Chunker().chunk(legacy, ChunkPolicy(strategy="character", size=4))
assert result.warnings[0].code == "source_mapping_unavailable"
assert all(ref.page is None for chunk in result.leaves for ref in chunk.source_refs)
```

## 流式分块示例

```python
from pathlib import Path

from agent_components.chunking import ChunkPolicy, StreamingChunker

streamer = StreamingChunker(window_chars=512)
policy = ChunkPolicy(strategy="character", size=160, overlap=20)
chunks = streamer.iter_file(
    Path("examples/data/contract.txt"),
    policy,
    document_id="purchase-contract",
)
for chunk in chunks:
    print(chunk.ordinal, chunk.start, chunk.end)

pieces = iter(["合同审批。", "交付验收。", "材料归档。"])
text_policy = ChunkPolicy(strategy="character", size=7)
for chunk in streamer.iter_text(pieces, text_policy, document_id="stream-demo"):
    print(chunk.start, chunk.end, chunk.text)
```

生产流式调用可按输入规模设置 `window_chars`、`max_input_chars` 和 `max_chunks`。`iter_text` 的每个输入片段应为字符串，片段边界可独立于最终的分块边界。

### 逐块写入 JSONL

下面用临时目录演示发布方式。`revision_id` 可显式使用业务版本，省略时计算解码后全文的摘要。及早退出时应关闭迭代器，释放快照临时文件。

```python
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_components.chunking import ChunkPolicy, StreamingChunker

streamer = StreamingChunker(window_chars=512, max_input_chars=100_000, max_chunks=1_000)
policy = ChunkPolicy(strategy="delimiter", size=160, delimiters=("\n\n", "。"))
with TemporaryDirectory() as directory:
    staging = Path(directory) / "chunks.tmp"
    published = Path(directory) / "chunks.jsonl"
    with (
        closing(streamer.iter_file(Path("examples/data/contract.txt"), policy,
                                   document_id="contract-001", revision_id="v3")) as chunks,
        staging.open("w", encoding="utf-8") as sink,
    ):
        for chunk in chunks:
            sink.write(chunk.model_dump_json() + "\n")
    staging.replace(published)
    assert published.stat().st_size > 0
```

### 跨窗口表格与资源上限

六种策略均可流式执行。Token 和语义策略由 `StreamingChunker(chunker=...)` 接收前面配置好的 `Chunker`。文本窗口和表格单行有独立限制，`table_overflow="allow"` 也不能绕过流式窗口的单行上限。流式返回逐个 `Chunk`，通过 `chunk.table.oversized` 查看允许的溢出。

```python
from agent_components.chunking import ChunkPolicy, StreamingChunker

header = "| 名称 | 金额 |\n| --- | --- |\n"
text = "前言\n" + header + "".join(f"| 项目{i} | {i} |\n" for i in range(30))
policy = ChunkPolicy(strategy="character", size=70)
streamer = StreamingChunker(window_chars=80, max_input_chars=5_000, max_chunks=100)
pieces = (text[start:start + 11] for start in range(0, len(text), 11))
cursor = final_row = 0
for chunk in streamer.iter_text(pieces, policy, document_id="contract-table"):
    assert chunk.start == cursor and chunk.text == text[chunk.start:chunk.end]
    cursor = chunk.end
    if chunk.table:
        assert len(chunk.index_text) <= policy.size
        assert chunk.table.row_start == final_row
        if final_row:
            assert chunk.context_prefix == header
        final_row = chunk.table.row_end
assert cursor == len(text) and final_row == 30

limited = StreamingChunker(max_input_chars=3)
try:
    next(limited.iter_text(["超过上限"], policy, document_id="too-large"))
except ValueError as exc:
    assert "max_input_chars" in str(exc)
else:
    raise AssertionError("应在产出首块前拒绝超限输入")
```

## 命令行复现

`examples/data` 提供六类 `policy-<strategy>.json`。下面分别运行本地文本、Word 章节和流式示例。Token 和语义可传 `--tokenizer-factory` / `--embedding-factory` 与相应版本 ID。[Provider 命令行配置](../../../docs/examples/chunking.md)

```sh
uv run --no-sync python -m examples.documents chunk examples/data/contract.txt --policy examples/data/policy-delimiter.json --output data/chunks.json
uv run --no-sync python -m examples.documents parse tests/fixtures/parent_child_sample.docx --output data/word.json
uv run --no-sync python -m examples.documents parents data/word.json --input-kind parsed --policy examples/data/policy-character.json --max-level 2 --output data/word-chunks.json
uv run --no-sync python -m examples.documents stream examples/data/contract.txt --policy examples/data/policy-character.json --output data/chunks.jsonl
```

实现入口见[普通与父子分块](chunker.py)、[流式分块](streaming.py)及[策略后端](backends.py)。
