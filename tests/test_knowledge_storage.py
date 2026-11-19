import pytest

from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import Document
from agent_components.core.errors import RevisionConflictError
from agent_components.retrieval import IndexMatch, IndexRecord, RetrievalPipeline


def revision(version, text):
    document = Document(id="contract", revision_id=version, text=text)
    return document, Chunker().chunk(document, ChunkPolicy(strategy="character", size=60))


def test_sqlite_keeps_drafts_and_published_versions_separate_after_reopen(tmp_path):
    from agent_components.knowledge_base import KnowledgeBase
    from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository

    path = tmp_path / "knowledge.db"
    repository = SQLiteKnowledgeRepository(path)
    knowledge = KnowledgeBase("contracts", repository)
    first, chunks = revision("r1", "尾款在验收后30天内支付。")
    knowledge.save_revision(first, chunks)
    knowledge.save_revision(first, chunks)
    assert knowledge.published_chunks(document_ids=["contract"]) == ()
    knowledge.publish_revision("contract", "r1", expected_revision=None)
    draft, draft_chunks = revision("r2", "尾款在复验通过后支付。")
    knowledge.save_revision(draft, draft_chunks)

    reopened = KnowledgeBase("contracts", SQLiteKnowledgeRepository(path))
    assert reopened.get_revision("contract", "r2").document.text == "尾款在复验通过后支付。"
    assert [chunk.text for chunk in reopened.published_chunks(document_ids=["contract"])] == [
        "尾款在验收后30天内支付。"
    ]
    assert reopened.published_chunks(document_ids=[]) == ()
    assert KnowledgeBase("other-base", repository).published_chunks(document_ids=["contract"]) == ()
    with pytest.raises(RevisionConflictError):
        reopened.publish_revision("contract", "r2", expected_revision=None)
    reopened.publish_revision("contract", "r2", expected_revision="r1")
    with pytest.raises(RevisionConflictError):
        knowledge.unpublish_document("contract", expected_revision="r1")
    assert [
        chunk.revision_id for chunk in knowledge.published_chunks(document_ids=["contract"])
    ] == ["r2"]
    knowledge.unpublish_document("contract", expected_revision="r2")
    assert reopened.published_chunks(document_ids=["contract"]) == ()
    assert reopened.get_revision("contract", "r1").document.text == first.text


def test_sqlite_rejects_conflicting_revision_or_chunks_without_changing_published_content(tmp_path):
    from agent_components.knowledge_base import KnowledgeBase
    from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository

    knowledge = KnowledgeBase("contracts", SQLiteKnowledgeRepository(tmp_path / "knowledge.db"))
    document, chunks = revision("r1", "验收后付款。")
    knowledge.save_revision(document, chunks)
    knowledge.publish_revision("contract", "r1", expected_revision=None)
    changed, changed_chunks = revision("r1", "签署后付款。")
    with pytest.raises(RevisionConflictError):
        knowledge.save_revision(changed, changed_chunks)
    with pytest.raises(ValueError, match="canonical text"):
        knowledge.save_revision(changed, chunks)
    with pytest.raises(LookupError):
        knowledge.publish_revision("contract", "missing", expected_revision="r1")
    assert [item.text for item in knowledge.published_chunks(document_ids=["contract"])] == [
        document.text
    ]


class LocalVectorAdapter:
    """声明独立于 LanceDB 的测试端口实现。"""

    dimensions = 2

    def __init__(self):
        self.rows = {}

    def upsert(self, records):
        self.rows.update({row.id: row for row in records})

    def recall(self, vector, *, limit, chunk_ids, include_vectors):
        from agent_components.retrieval.vector_store import VectorHit

        return tuple(
            VectorHit(
                match=IndexMatch(
                    id=row.id,
                    document_id=row.document_id,
                    revision_id=row.revision_id,
                    text=row.text,
                    score=score,
                    score_kind="similarity",
                ),
                vector=row.vector if include_vectors else None,
            )
            for score, row in sorted(
                (
                    (sum(a * b for a, b in zip(vector, row.vector, strict=True)), row)
                    for row in self.rows.values()
                    if row.id in chunk_ids
                ),
                key=lambda entry: entry[0],
                reverse=True,
            )[:limit]
        )


class QueryProvider:
    dimension = 2

    def embed_batch(self, texts):
        return [(1.0, 0.0) for _ in texts]


def test_config_switches_content_and_vector_adapters_independently(tmp_path):
    from agent_components.knowledge_base import KnowledgeBase
    from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository
    from agent_components.retrieval import RetrievalOptions
    from agent_components.storage.backends import StorageBinding, StorageRouter

    west = SQLiteKnowledgeRepository(tmp_path / "west.db")
    east = SQLiteKnowledgeRepository(tmp_path / "east.db")
    normal_vectors, replacement_vectors = LocalVectorAdapter(), LocalVectorAdapter()
    router = StorageRouter(
        documents={"west": west, "east": east},
        vectors={"normal": normal_vectors, "replacement": replacement_vectors},
    )
    for name, store, text, adapter in [
        ("r1", west, "尾款在验收后支付。", normal_vectors),
        ("r2", east, "尾款在复验后支付。", replacement_vectors),
    ]:
        document, chunks = revision(name, text)
        kb = KnowledgeBase("contracts", store)
        kb.save_revision(document, chunks)
        chunk = chunks.leaves[0]
        adapter.upsert(
            [
                IndexRecord(
                    id=chunk.id,
                    document_id=chunk.document_id,
                    revision_id=chunk.revision_id,
                    text=chunk.index_text,
                    vector=(1.0, 0.0),
                )
            ]
        )
        kb.publish_revision("contract", name, expected_revision=None)

    for config, expected in [
        ('{"documents":"west","vectors":"normal"}', ["尾款在验收后支付。"]),
        ('{"documents":"east","vectors":"normal"}', []),
        ('{"documents":"east","vectors":"replacement"}', ["尾款在复验后支付。"]),
        ('{"documents":"west","vectors":"replacement"}', []),
    ]:
        stores = router.resolve(StorageBinding.model_validate_json(config))
        kb = KnowledgeBase("contracts", stores.documents)
        result = RetrievalPipeline(stores.vectors, QueryProvider()).search(
            "尾款什么时候支付？",
            visible_chunks=kb.published_chunks(document_ids=["contract"]),
            options=RetrievalOptions(selection="mmr"),
        )
        assert [window.text for window in result.context.windows] == expected
        assert all(match.score_kind == "similarity" for match in result.matches)

    with pytest.raises(KeyError, match="vectors"):
        router.resolve(StorageBinding(documents="east", vectors="unregistered"))


@pytest.mark.parametrize("bad_number", [float("nan"), float("inf")])
def test_sqlite_rejects_lossy_payload_before_creating_revision(tmp_path, bad_number):
    from agent_components.knowledge_base import KnowledgeBase
    from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository

    knowledge = KnowledgeBase("contracts", SQLiteKnowledgeRepository(tmp_path / "knowledge.db"))
    document, chunks = revision("r1", "验收后付款。")
    invalid_document = document.model_copy(update={"metadata": {"amount": bad_number}})
    with pytest.raises(ValueError, match="round-trip"):
        knowledge.save_revision(invalid_document, chunks)
    assert knowledge.get_revision("contract", "r1") is None
    knowledge.save_revision(document, chunks)
    knowledge.publish_revision("contract", "r1", expected_revision=None)
    assert knowledge.published_chunks(document_ids=["contract"]) == chunks.leaves


@pytest.mark.integration
def test_preregistered_lancedb_reader_sees_other_handle_writes(tmp_path):
    pytest.importorskip("lancedb")
    from agent_components.retrieval.adapters.lancedb import LanceDBVectorStore

    root = tmp_path / "index"
    writer = LanceDBVectorStore(root, dimensions=2)
    reader = LanceDBVectorStore(root, dimensions=2)
    record = IndexRecord(
        id="term", document_id="contract", revision_id="r1", text="验收后付款。", vector=(1.0, 0.0)
    )
    args = {"limit": 1, "chunk_ids": [record.id], "include_vectors": True}
    assert reader.recall((1.0, 0.0), **args) == ()
    writer.upsert([record])
    hits = reader.recall((1.0, 0.0), **args)
    assert [(hit.match.id, hit.vector) for hit in hits] == [(record.id, (1.0, 0.0))]
    for scope in (record.id, None, {record.id: False}):
        with pytest.raises(TypeError, match="filter IDs"):
            reader.recall((1.0, 0.0), limit=1, chunk_ids=scope, include_vectors=False)


def test_revision_idempotency_preserves_json_types_and_ignores_key_order(tmp_path):
    from agent_components.knowledge_base import KnowledgeBase
    from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository

    knowledge = KnowledgeBase("contracts", SQLiteKnowledgeRepository(tmp_path / "knowledge.db"))
    document, chunks = revision("r1", "验收后付款。")
    original = document.model_copy(update={"metadata": {"approved": True, "amount": 3200}})
    reordered = document.model_copy(update={"metadata": {"amount": 3200, "approved": True}})
    different_type = document.model_copy(update={"metadata": {"amount": 3200, "approved": 1}})
    knowledge.save_revision(original, chunks)
    knowledge.save_revision(reordered, chunks)
    with pytest.raises(RevisionConflictError):
        knowledge.save_revision(different_type, chunks)
    assert knowledge.get_revision("contract", "r1").document.metadata["approved"] is True


def test_content_scope_rejects_scalar_strings_instead_of_splitting_ids(tmp_path):
    from agent_components.knowledge_base import KnowledgeBase
    from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository

    repository = SQLiteKnowledgeRepository(tmp_path / "knowledge.db")
    knowledge = KnowledgeBase("contracts", repository)
    for doc_id in ("a", "ab"):
        document = Document(id=doc_id, revision_id="r1", text="验收后付款。")
        result = Chunker().chunk(document, ChunkPolicy(strategy="character"))
        knowledge.save_revision(document, result)
        knowledge.publish_revision(doc_id, "r1", expected_revision=None)

    for scope in ("ab", {"ab": False}, None):
        with pytest.raises(TypeError, match="document IDs"):
            knowledge.published_chunks(document_ids=scope)
        with pytest.raises(TypeError, match="document IDs"):
            repository.read_published("contracts", document_ids=scope)
    assert [chunk.document_id for chunk in knowledge.published_chunks(document_ids=["ab"])] == [
        "ab"
    ]
