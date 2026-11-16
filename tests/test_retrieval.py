import pytest

pytest.importorskip("lancedb")

from agent_components.retrieval import IndexRecord, LanceDBIndex

pytestmark = pytest.mark.integration


def test_embedded_upsert_query_dimension_and_document_filter(tmp_path):
    index = LanceDBIndex(tmp_path / "index", dimensions=2)
    records = [
        IndexRecord(id="a", document_id="doc-a", revision_id="r1", text="first", vector=(1.0, 0.0)),
        IndexRecord(
            id="b", document_id="doc-b", revision_id="r1", text="second", vector=(0.0, 1.0)
        ),
    ]
    index.upsert(records)
    assert index.search_vector([1.0, 0.0])[0].id == "a"
    assert index.search_vector([1.0, 0.0], document_ids=["doc-b"])[0].id == "b"
    assert index.search_vector([1.0, 0.0], document_ids=[]) == ()
    index.upsert([records[0].model_copy(update={"text": "updated"})])
    assert index.search_vector([1.0, 0.0])[0].text == "updated"
    with pytest.raises(ValueError):
        index.search_vector([1.0])
    with pytest.raises(ValueError):
        LanceDBIndex(tmp_path / "index", dimensions=3)
