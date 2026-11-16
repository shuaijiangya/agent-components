import math

import pytest

from agent_components.chunking import Chunker, ChunkPolicy, ParentChildBuilder
from agent_components.core.contracts import Document, DocumentElement, SourceRef, SourceSpan
from agent_components.sections import SectionBuilder
from agent_components.sources import SourceMapper


class ByteEncoder:
    def encode(self, text):
        return list(text.encode("utf-8"))


class TopicEmbedding:
    dimension = 3

    def __init__(self):
        self.batches = []

    def embed_batch(self, texts):
        self.batches.append(list(texts))
        return [
            [
                1 + sum(t.count(w) for w in ("付款", "金额", "发票")),
                1 + sum(t.count(w) for w in ("库存", "仓库", "物流")),
                1,
            ]
            for t in texts
        ]


def assert_coverage(doc, result, size, measure=len):
    cursor = 0
    for chunk in result.leaves:
        assert chunk.start <= cursor < chunk.end
        assert chunk.text == doc.text[chunk.start : chunk.end]
        assert measure(chunk.index_text) <= size
        cursor = chunk.end
    assert cursor == len(doc.text)


@pytest.mark.parametrize("include", ["previous", "next"])
def test_literal_delimiters_keep_repeated_unicode_and_regex_characters(include):
    text = "开头.*正文.*重复||重复||尾部"
    doc = Document(id="delimiter", text=text)
    result = Chunker().chunk(
        doc,
        ChunkPolicy(
            strategy="delimiter", size=100, delimiters=(".*", "||"), include_delimiter=include
        ),
    )
    assert "".join(c.text for c in result.leaves) == text
    if include == "previous":
        assert result.leaves[0].text == "开头.*"
    else:
        assert result.leaves[0].text == "开头"
        assert result.leaves[1].text.startswith(".*")
    assert_coverage(doc, result, 100)


@pytest.mark.parametrize(
    "settings",
    [
        {"delimiters": ("",)},
        {"delimiters": ()},
        {"delimiters": ("x", "x")},
        {"recursive_delimiters": ()},
        {"recursive_delimiters": ((),)},
        {"sentences_per_chunk": 0},
        {"semantic_threshold": float("nan")},
        {"embedding_batch_size": 0},
        {"semantic_window_chars": 0},
        {"table_overflow": "split_row"},
    ],
)
def test_extended_policy_rejects_ambiguous_or_unsafe_values(settings):
    with pytest.raises(ValueError):
        ChunkPolicy(**settings)


@pytest.mark.parametrize("overlap", [0, 3])
def test_real_token_budget_preserves_chinese_emoji_and_all_whitespace(overlap):
    doc = Document(id="tokens", text="付款金额🙂中文\r\n  合同义务与发票。" * 8)
    encoder = ByteEncoder()
    chunker = Chunker(tokenizer=encoder, tokenizer_id="utf8-byte-v1")
    result = chunker.chunk(doc, ChunkPolicy(strategy="token", size=23, overlap=overlap))
    assert_coverage(doc, result, 23, lambda text: len(encoder.encode(text)))
    assert all("\ufffd" not in c.text for c in result.leaves)
    assert result == chunker.chunk(doc, ChunkPolicy(strategy="token", size=23, overlap=overlap))
    if overlap == 0:
        assert "".join(c.text for c in result.leaves) == doc.text


def test_token_budget_and_backend_identity_are_enforced():
    doc = Document(id="t", text="中")
    with pytest.raises(ValueError, match="tokenizer"):
        Chunker().chunk(doc, ChunkPolicy(strategy="token"))
    with pytest.raises(ValueError, match="together"):
        Chunker(tokenizer=ByteEncoder())
    with pytest.raises(ValueError, match="budget"):
        Chunker(tokenizer=ByteEncoder(), tokenizer_id="bytes").chunk(
            doc, ChunkPolicy(strategy="token", size=1)
        )
    one = Chunker(tokenizer=ByteEncoder(), tokenizer_id="v1").chunk(
        doc, ChunkPolicy(strategy="token", size=3)
    )
    two = Chunker(tokenizer=ByteEncoder(), tokenizer_id="v2").chunk(
        doc, ChunkPolicy(strategy="token", size=3)
    )
    assert one.chunks[0].id != two.chunks[0].id


@pytest.mark.integration
def test_offline_native_bpe_tokenizer_with_actual_token_budget():
    tokenizers = pytest.importorskip("tokenizers")
    from tokenizers.models import BPE
    from tokenizers.pre_tokenizers import ByteLevel
    from tokenizers.trainers import BpeTrainer

    tokenizer = tokenizers.Tokenizer(BPE(unk_token="[UNK]"))
    tokenizer.pre_tokenizer = ByteLevel(add_prefix_space=False)
    tokenizer.train_from_iterator(
        ["支付金额为3200元。仓库库存和物流。🙂"] * 20,
        BpeTrainer(vocab_size=300, special_tokens=["[UNK]"], initial_alphabet=ByteLevel.alphabet()),
    )

    class Encoder:
        def encode(self, text):
            return tokenizer.encode(text, add_special_tokens=False).ids

    doc = Document(id="bpe", text="支付金额为3200元。\n🙂未知中文及全部空格  " * 20)
    result = Chunker(tokenizer=Encoder(), tokenizer_id="fixture-bpe-v1").chunk(
        doc, ChunkPolicy(strategy="token", size=12)
    )
    assert_coverage(doc, result, 12, lambda value: len(Encoder().encode(value)))
    assert "".join(c.text for c in result.leaves) == doc.text


@pytest.mark.integration
@pytest.mark.parametrize("strategy", ["recursive", "sentence"])
def test_chonkie_extended_rules_and_overlap(strategy):
    pytest.importorskip("chonkie")
    doc = Document(id="rules", text="甲乙。\n丙丁。第二条付款义务。" * 5)
    result = Chunker().chunk(doc, ChunkPolicy(strategy=strategy, size=9, overlap=2))
    assert_coverage(doc, result, 9)
    assert any(a.end > b.start for a, b in zip(result.leaves, result.leaves[1:], strict=False))
    if strategy == "recursive":
        customized = Chunker().chunk(
            Document(id="c", text="aaaa@@bbbb@@cccc"),
            ChunkPolicy(size=8, recursive_delimiters=(("@@",),)),
        )
        assert customized.leaves[0].text == "aaaa@@"


@pytest.mark.integration
def test_sentence_strategy_keeps_single_sentences_and_splits_overlong_sentence():
    pytest.importorskip("chonkie")
    result = Chunker().chunk(
        Document(id="s", text="第一句。第二句！第三句？"),
        ChunkPolicy(strategy="sentence", size=100),
    )
    assert [c.text for c in result.leaves] == ["第一句。", "第二句！", "第三句？"]
    long_doc = Document(id="long", text="长" * 25 + "。\r\n  ")
    result = Chunker().chunk(long_doc, ChunkPolicy(strategy="sentence", size=5))
    assert_coverage(long_doc, result, 5)
    assert "".join(c.text for c in result.leaves) == long_doc.text


@pytest.mark.integration
def test_semantic_uses_native_chonkie_and_bounded_injected_embeddings():
    pytest.importorskip("chonkie")
    embedding = TopicEmbedding()
    doc = Document(id="semantic", text=("付款金额与发票。" * 10) + ("仓库库存和物流。" * 10))
    chunker = Chunker(embedding=embedding, embedding_id="topic-fixture-v1")
    result = chunker.chunk(
        doc,
        ChunkPolicy(
            strategy="semantic",
            size=64,
            similarity_window=1,
            embedding_batch_size=2,
            semantic_window_chars=150,
        ),
    )
    assert_coverage(doc, result, 64)
    assert "".join(c.text for c in result.leaves) == doc.text
    assert embedding.batches and all(len(batch) <= 2 for batch in embedding.batches)
    assert len(result.leaves) > 1
    assert any(c.end == 80 for c in result.leaves)


@pytest.mark.integration
@pytest.mark.parametrize("invalid", ["count", "dimension", "nan", "zero"])
def test_semantic_rejects_invalid_embedding_outputs(invalid):
    pytest.importorskip("chonkie")

    class InvalidEmbedding(TopicEmbedding):
        def embed_batch(self, texts):
            if invalid == "count":
                return []
            vector = {"dimension": [1], "nan": [math.nan, 1, 2], "zero": [0, 0, 0]}[invalid]
            return [vector for _ in texts]

    with pytest.raises(ValueError, match="embedding"):
        Chunker(embedding=InvalidEmbedding(), embedding_id="invalid-v1").chunk(
            Document(id="bad", text="付款金额。库存物流。" * 20),
            ChunkPolicy(strategy="semantic", similarity_window=1),
        )


def test_structure_and_source_map_validation_and_intersection():
    ref = SourceRef(
        input_id="doc", element_id="#/texts/0", page=2, bbox=(0, 0, 100, 200), precision="element"
    )
    doc = Document(
        id="doc", text="前言正文一正文二", source_map=(SourceSpan(start=2, end=8, refs=(ref,)),)
    )
    refs = SourceMapper(doc).resolve(3, 6)
    assert refs[0].precision == "text"
    assert refs[1].element_id == "#/texts/0" and refs[1].page == 2
    assert (refs[1].start, refs[1].end) == (3, 6)
    with pytest.raises(ValueError):
        Document(id="bad", text="abc", source_map=(SourceSpan(start=0, end=4, refs=(ref,)),))
    with pytest.raises(ValueError):
        DocumentElement(id="bad", kind="heading", start=0, end=1)


def test_sections_use_native_levels_preserve_preamble_and_hierarchical_path():
    text = "前言\n第一章\n条款A\n第一节\n条款B\n第二章\n条款C"
    headings = []
    for title, level in [("第一章", 1), ("第一节", 2), ("第二章", 1)]:
        start = text.index(title)
        headings.append(
            DocumentElement(
                id=title,
                kind="heading",
                start=start,
                end=start + len(title),
                level=level,
                title=title,
            )
        )
    doc = Document(id="sections", text=text, elements=tuple(headings))
    sections = SectionBuilder().build(doc)
    assert [s.path for s in sections] == [(), ("第一章",), ("第一章", "第一节"), ("第二章",)]
    assert "".join(text[s.start : s.end] for s in sections) == text
    shallow = SectionBuilder().build(doc, max_level=1, include_preamble=False)
    assert len(shallow) == 2 and shallow[0].end == text.index("第二章")
    result = ParentChildBuilder().build_sections(doc, ChunkPolicy(strategy="character", size=4))
    assert len(result.parents) == 4
    for leaf in result.leaves:
        assert leaf.section_path == next(
            p.section_path for p in result.parents if p.id == leaf.parent_id
        )
    assert len(SectionBuilder().build(Document(id="plain", text="# 正文不是结构标题"))) == 1


TABLE = "| 名称 | 金额 |\n| --- | --- |\n" + "".join(
    f"| 项目{i:02} | {i * 100} |\n" for i in range(12)
)


def test_table_is_split_by_complete_rows_with_independent_repeated_header():
    doc = Document(id="table", text="前言\n\n" + TABLE + "\n结论")
    result = Chunker().chunk(doc, ChunkPolicy(strategy="character", size=70))
    assert "".join(c.text for c in result.leaves) == doc.text
    fragments = [c for c in result.leaves if c.table]
    assert len(fragments) > 1
    for index, chunk in enumerate(fragments):
        assert len(chunk.index_text) <= 70
        assert chunk.text == doc.text[chunk.start : chunk.end]
        if index:
            assert chunk.context_prefix == "| 名称 | 金额 |\n| --- | --- |\n"
            assert chunk.text.startswith("| 项目")
    assert fragments[0].table.row_start == 0 and fragments[-1].table.row_end == 12
    assert all(
        a.table.row_end == b.table.row_start for a, b in zip(fragments, fragments[1:], strict=False)
    )


def test_table_overflow_is_explicit_and_disabling_preservation_uses_normal_budget():
    doc = Document(id="wide", text="| a | b |\n| --- | --- |\n| " + "长" * 100 + " | 1 |\n")
    with pytest.raises(ValueError, match="table"):
        Chunker().chunk(doc, ChunkPolicy(strategy="character", size=40))
    allowed = Chunker().chunk(
        doc, ChunkPolicy(strategy="character", size=40, table_overflow="allow")
    )
    assert any(c.table.oversized for c in allowed.leaves if c.table)
    assert allowed.warnings[0].code == "oversized_table_row"
    normal = Chunker().chunk(doc, ChunkPolicy(strategy="character", size=40, preserve_tables=False))
    assert all(len(c.text) <= 40 for c in normal.leaves)
    assert not any(c.table for c in normal.leaves)


def test_fenced_markdown_table_is_plain_code():
    doc = Document(id="code", text="```markdown\n" + TABLE + "```\n")
    result = Chunker().chunk(doc, ChunkPolicy(strategy="character", size=17))
    assert not any(c.table for c in result.leaves)
    assert "".join(c.text for c in result.leaves) == doc.text


def test_single_column_table_and_token_table_budget():
    doc = Document(id="one-column", text="| 项目 |\n| --- |\n" + "| 中文🙂 |\n" * 10)
    chunker = Chunker(tokenizer=ByteEncoder(), tokenizer_id="bytes-v1")
    result = chunker.chunk(doc, ChunkPolicy(strategy="token", size=70))
    assert any(c.table for c in result.leaves)
    assert_coverage(doc, result, 70, lambda text: len(ByteEncoder().encode(text)))


def test_parent_table_protection_is_preflight_and_warning_ids_are_global():
    table = "| a | b |\n| --- | --- |\n| " + "x" * 100 + " | 1 |\n"
    doc = Document(id="parent-table", text="intro\n" + table)

    class Spy(Chunker):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def chunk(self, document, policy=None):
            self.calls += 1
            return super().chunk(document, policy)

    spy = Spy()
    with pytest.raises(ValueError, match="preserved table"):
        ParentChildBuilder(spy).build(doc, [(0, 6), (6, 30)], ChunkPolicy(strategy="character"))
    assert spy.calls == 0
    result = ParentChildBuilder().build(
        doc,
        [(6, len(doc.text))],
        ChunkPolicy(strategy="character", size=30, table_overflow="allow"),
    )
    assert all(w.input_id in {c.id for c in result.chunks} for w in result.warnings)


@pytest.mark.parametrize("fence", ["````markdown\n```\n", "~~~markdown\n```\n"])
def test_mismatched_or_shorter_fence_does_not_expose_code_as_table(fence):
    doc = Document(id="fence", text=fence + TABLE)
    result = Chunker().chunk(doc, ChunkPolicy(strategy="character", size=20))
    assert not any(c.table for c in result.leaves)
