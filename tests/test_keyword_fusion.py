import pytest

pytest.importorskip("lancedb")

from agent_components.retrieval.adapters.lancedb import LanceDBVectorStore
from agent_components.retrieval.fusion import fuse_ranks
from agent_components.retrieval.vector_store import IndexRecord

pytestmark = pytest.mark.integration


def _record(chunk_id, text):
    return IndexRecord(
        id=chunk_id, document_id="guide", revision_id="r1", text=text, vector=(1.0, 0.0)
    )


def test_keyword_recall_filters_chinese_bm25_before_top_k(tmp_path):
    store = LanceDBVectorStore(tmp_path / "index", dimensions=2)
    store.upsert(
        [
            _record("hidden", "高血压"),
            _record("weak", "本医院发布新版高血压患者饮食和运动指南"),
            _record("allowed'quote", "高血压用药指南"),
            _record("unrelated", "如何申请跨境旅行保险"),
        ]
    )

    assert store.recall_keywords("高血压", limit=1, chunk_ids=["hidden", "weak"])[0].id == "hidden"
    scope = ["weak", "allowed'quote", "unrelated"]
    hits = store.recall_keywords("高血压", limit=2, chunk_ids=scope)
    assert [hit.id for hit in hits] == ["allowed'quote", "weak"]
    assert all(hit.score_kind == "bm25" for hit in hits)
    assert hits[0].score > hits[1].score > 0
    assert store.recall_keywords("高血压", limit=1, chunk_ids=scope) == hits[:1]
    assert store.recall_keywords('"高血压用药"', limit=1, chunk_ids=scope)[0].id == "allowed'quote"
    assert store.recall_keywords("高血压", limit=2, chunk_ids=[]) == ()


def test_keyword_recall_upgrades_existing_index_and_sees_new_writes(tmp_path):
    import lancedb
    from lancedb.index import FTS

    path = tmp_path / "index"
    writer = LanceDBVectorStore(path, dimensions=2)
    writer.upsert([_record("guide", "本手册包括高血压用药指南")])
    lancedb.connect(path).open_table("chunks").create_index("text", config=FTS())
    reader = LanceDBVectorStore(path, dimensions=2)

    assert reader.recall_keywords("高血压", limit=1, chunk_ids=["guide"])[0].id == "guide"
    writer.upsert([_record("guide", "医院发布过敏性鼻炎诊疗手册")])
    assert reader.recall_keywords("高血压", limit=1, chunk_ids=["guide"]) == ()
    assert reader.recall_keywords("鼻炎", limit=1, chunk_ids=["guide"])[0].id == "guide"
    writer.build_fts()
    assert reader.recall_keywords("鼻炎", limit=1, chunk_ids=["guide"])[0].id == "guide"
    assert reader.search_vector([1.0, 0.0])[0].id == "guide"
    writer.delete_records(chunk_ids=["guide"])
    assert reader.recall_keywords("鼻炎", limit=1, chunk_ids=["guide"]) == ()


def test_managed_records_lookup_and_delete_only_requested_chunks(tmp_path):
    store = LanceDBVectorStore(tmp_path / "index", dimensions=2)
    keep = _record("keep", "保留")
    remove = _record("remove'quote", "删除")
    store.upsert([keep, remove])

    assert store.get_records(chunk_ids=["missing", remove.id, remove.id]) == (remove,)
    store.delete_records(chunk_ids=[])
    store.delete_records(chunk_ids=["missing') OR 1=1 --"])
    assert store.get_records(chunk_ids=[keep.id]) == (keep,)
    store.delete_records(chunk_ids=[remove.id])
    store.delete_records(chunk_ids=[remove.id])
    assert store.get_records(chunk_ids=[keep.id, remove.id]) == (keep,)
    assert store.get_records(chunk_ids=[]) == ()


@pytest.mark.parametrize("chunk_ids", [None, "keep", b"keep", [1]])
def test_scoped_methods_reject_invalid_chunk_id_sequences(tmp_path, chunk_ids):
    store = LanceDBVectorStore(tmp_path / "index", dimensions=2)
    with pytest.raises(TypeError):
        store.recall_keywords("高血压", limit=1, chunk_ids=chunk_ids)
    with pytest.raises(TypeError):
        store.get_records(chunk_ids=chunk_ids)
    with pytest.raises(TypeError):
        store.delete_records(chunk_ids=chunk_ids)


def test_fusion_preserves_first_seen_ties_and_counts_each_id_once_per_channel():
    results = fuse_ranks(
        ["vector", "both", "vector", "vector-late"],
        ["keyword", "both", "keyword", "keyword-late"],
        k=5,
    )
    assert [chunk_id for chunk_id, _ in results] == [
        "both",
        "vector",
        "keyword",
        "vector-late",
        "keyword-late",
    ]
    assert [score for _, score in results] == pytest.approx([2 / 7, 1 / 6, 1 / 6, 1 / 8, 1 / 8])


def test_fusion_accepts_empty_channels():
    assert fuse_ranks([], []) == ()
    ((chunk_id, score),) = fuse_ranks([], ["keyword"])
    assert chunk_id == "keyword"
    assert score == pytest.approx(1 / 61)
    with pytest.raises(ValueError):
        fuse_ranks([], [], k=0)
