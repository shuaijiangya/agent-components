from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.streaming import StreamingChunker


def test_stream_preserves_unicode_crlf_global_offsets_and_transport_independent_identity(tmp_path):
    text = "🙂中文\r\n  原文" * 100
    policy = ChunkPolicy(strategy="character", size=11, overlap=2)
    stream = StreamingChunker(window_chars=79)
    one = list(stream.iter_text([text], policy, document_id="stream"))
    two = list(
        stream.iter_text(
            (text[i : i + 3] for i in range(0, len(text), 3)), policy, document_id="stream"
        )
    )
    assert one == two
    path = tmp_path / "text.txt"
    path.write_bytes(b"\xef\xbb\xbf" + text.encode())
    assert one == list(stream.iter_file(path, policy, document_id="stream"))
    cursor = 0
    for chunk in one:
        assert chunk.start <= cursor < chunk.end
        assert chunk.text == text[chunk.start : chunk.end]
        assert chunk.source_refs[0].start == chunk.start
        cursor = chunk.end
    assert cursor == len(text)
    assert [c.ordinal for c in one] == list(range(len(one)))


@pytest.mark.integration
@pytest.mark.parametrize("strategy", ["recursive", "sentence", "delimiter", "token", "semantic"])
def test_every_strategy_streams_with_valid_global_positions(strategy):
    pytest.importorskip("chonkie")
    from test_chunking_extended import ByteEncoder, TopicEmbedding

    text = "付款金额与发票。仓库库存和物流。" * 50
    chunker = Chunker(
        tokenizer=ByteEncoder(),
        tokenizer_id="bytes-v1",
        embedding=TopicEmbedding(),
        embedding_id="topic-fixture-v1",
    )
    policy = ChunkPolicy(strategy=strategy, size=32, similarity_window=1)
    result = list(
        StreamingChunker(chunker, window_chars=87).iter_text([text], policy, document_id="all")
    )
    assert "".join(c.text for c in result) == text
    assert all(c.text == text[c.start : c.end] for c in result)
    assert all((len(c.text.encode()) if strategy == "token" else len(c.text)) <= 32 for c in result)


def test_streaming_long_table_preserves_every_row_across_analysis_windows():
    header = "| 名称 | 金额 |\n| --- | --- |\n"
    table = header + "".join(f"| 项目{i} | {i} |\n" for i in range(200))
    text = "前言\n" + table + "\n尾部"
    result = list(
        StreamingChunker(window_chars=100).iter_text(
            [text], ChunkPolicy(strategy="character", size=70), document_id="table"
        )
    )
    assert "".join(c.text for c in result) == text
    assert all(c.text == text[c.start : c.end] for c in result)
    tables = [c for c in result if c.table]
    assert len(tables) > 20
    assert tables[-1].table.row_end == 200
    assert all(c.context_prefix == header for c in tables[1:])
    assert all(c.table.header_start == 3 for c in tables)
    assert all(len(c.index_text) <= 70 for c in result)


def test_streaming_rejects_resource_and_unbounded_row_limits():
    policy = ChunkPolicy(strategy="character", size=4)
    with pytest.raises(ValueError, match="max_input_chars"):
        list(StreamingChunker(max_input_chars=3).iter_text(["abcd"], policy, document_id="limited"))
    with pytest.raises(ValueError, match="max_chunks"):
        list(StreamingChunker(max_chunks=1).iter_text(["abcdef"], policy, document_id="limited"))
    with pytest.raises(ValueError, match="strings"):
        list(StreamingChunker().iter_text([None], policy, document_id="invalid"))
    wide = "| a | b |\n| --- | --- |\n| " + "x" * 200 + " | 1 |\n"
    with pytest.raises(ValueError, match="window"):
        list(
            StreamingChunker(window_chars=60).iter_text(
                [wide], ChunkPolicy(strategy="character", size=500), document_id="wide"
            )
        )


def test_streaming_shared_component_is_deterministic_under_threads():
    component = StreamingChunker(window_chars=97)
    policy = ChunkPolicy(strategy="character", size=23)
    text = "合同付款、交付和验收。" * 500

    def run(_):
        return [c.id for c in component.iter_text([text], policy, document_id="shared")]

    baseline = run(0)
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(ids == baseline for ids in executor.map(run, range(16)))


def test_stream_fence_tracking_and_window_limit_for_oversized_table_budget():
    code = "~~~~markdown\n```\n| a | b |\n| --- | --- |\n| v | 1 |\n~~~~\n"
    result = list(
        StreamingChunker(window_chars=80).iter_text(
            [code], ChunkPolicy(strategy="character", size=20), document_id="code"
        )
    )
    assert not any(c.table for c in result)
    assert "".join(c.text for c in result) == code
    table = "| a | b |\n| --- | --- |\n" + "| value | 1 |\n" * 1000
    pieces = list(
        StreamingChunker(window_chars=80).iter_text(
            [table], ChunkPolicy(strategy="character", size=100000), document_id="bounded"
        )
    )
    assert len(pieces) > 100
    assert max(len(c.index_text) for c in pieces) <= 80
    assert "".join(c.text for c in pieces) == table


def test_character_overlap_is_preserved_at_streaming_window_seams():
    text = "普通文本与窗口边界🙂" * 100
    result = list(
        StreamingChunker(window_chars=37).iter_text(
            [text], ChunkPolicy(strategy="character", size=11, overlap=3), document_id="seam"
        )
    )
    for before, after in zip(result, result[1:], strict=False):
        assert before.end - after.start == 3
        assert text[before.start : before.end] == before.text
        assert text[after.start : after.end] == after.text
    assert result[-1].end == len(text)
