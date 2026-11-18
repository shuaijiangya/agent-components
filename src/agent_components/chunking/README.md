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

以下 Python 示例可分别在仓库根目录运行。要运行全部示例，安装 `chunking` 和 `parsing` 依赖组。后者提供 Word 解析和 Token 示例使用的 `tokenizers`。

```sh
uv sync --locked --extra chunking --extra parsing
```

如果使用已激活的 Conda 环境，从仓库根目录运行 `UV_PROJECT_ENVIRONMENT="$CONDA_PREFIX" uv sync --locked --inexact --extra chunking --extra parsing`。

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

for policy in (single, multiple):
    result = chunker.chunk(document, policy)
    print([chunk.text for chunk in result.leaves])
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

## 手动指定父块范围

已有业务章节边界时，每组 `[start, end)` 对应一个父块。子策略分别在每个父块内部生效。

```python
from agent_components.chunking import ChunkPolicy, ParentChildBuilder
from agent_components.core.contracts import Document

document = Document(id="manual-demo", text="采购审批与签署\n到货核验与归档")
boundary = document.text.index("到货")
result = ParentChildBuilder().build(
    document,
    [(0, boundary), (boundary, len(document.text))],
    ChunkPolicy(strategy="character", size=4),
)

parents = {parent.id: parent for parent in result.parents}
for child in result.leaves:
    parent = parents[child.parent_id]
    assert parent.start <= child.start < child.end <= parent.end
    print(parent.text, "→", child.text)
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
print(repr(table_chunks[1].text))
print(table_chunks[1].index_text)
assert table_chunks[1].context_prefix == header

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

实现入口见[普通与父子分块](chunker.py)、[流式分块](streaming.py)及[策略后端](backends.py)。
