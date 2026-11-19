import sqlite3
from contextlib import closing
from hashlib import sha256
from shutil import copyfile

import pytest

from agent_components.chunking import Chunker, ChunkPolicy
from agent_components.core.contracts import Document
from agent_components.core.errors import RevisionConflictError
from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository
from agent_components.knowledge_base.contracts import KnowledgeRevision


def catalog(kb_id="contracts", index_id="primary"):
    from agent_components.knowledge_base.lifecycle import Catalog

    return Catalog(kb_id, "Contracts", index_id, "embedding")


def revision(doc_id="document", revision_id="r1", text="Payment is due after acceptance."):
    document = Document(id=doc_id, revision_id=revision_id, text=text)
    chunks = Chunker().chunk(document, ChunkPolicy(strategy="character", size=40))
    return KnowledgeRevision(document=document, chunks=chunks)


def artifacts(data=b"source"):
    from agent_components.knowledge_base.artifacts import ArtifactRef
    from agent_components.knowledge_base.lifecycle import RevisionArtifacts

    return RevisionArtifacts(
        *(
            ArtifactRef(kind=kind, sha256=sha256(data).hexdigest(), size=len(data))
            for kind in ("source", "parsed", "markdown")
        )
    )


def unexpected_call(*args):
    pytest.fail("a rejected or repeated operation invoked its side effect")


def setup_index_job(repo, kb_id="contracts"):
    content = revision()
    repo.save_managed_revision(kb_id, content, artifacts)
    return repo.enqueue_index(kb_id, "document", "r1")


def test_catalog_compare_and_set_and_unique_active_index(tmp_path):
    repo = SQLiteKnowledgeRepository(tmp_path / "knowledge.db")
    with pytest.raises(LookupError):
        repo.update_catalog("missing", "New name", expected_version=1)

    original = catalog()
    repo.create_catalog(original)
    with pytest.raises(RevisionConflictError):
        repo.create_catalog(catalog("another"))
    renamed = repo.update_catalog("contracts", "New name", expected_version=1)
    assert (renamed.name, renamed.version, renamed.index_id, renamed.embedding_id) == (
        "New name",
        2,
        "primary",
        "embedding",
    )
    assert repo.get_catalog("contracts") == renamed
    assert renamed.generation
    assert repo.list_catalogs() == (renamed,)
    with pytest.raises(RevisionConflictError):
        repo.delete_catalog("contracts", expected_version=1)
    repo.delete_catalog("contracts", expected_version=2)
    assert repo.list_catalogs() == ()


def test_catalog_does_not_adopt_legacy_content(tmp_path):
    repo = SQLiteKnowledgeRepository(tmp_path / "knowledge.db")
    repo.save_revision("contracts", revision())
    repo.publish_revision("contracts", "document", "r1", expected_revision=None)
    with pytest.raises(RevisionConflictError):
        repo.create_catalog(catalog())
    assert repo.get_catalog("contracts") is None
    assert repo.read_published("contracts", document_ids=["document"]) == (revision(),)


def test_managed_revision_cannot_be_replaced_or_published_through_legacy_api(tmp_path):
    path = tmp_path / "knowledge.db"
    repo = SQLiteKnowledgeRepository(path)
    repo.create_catalog(catalog())
    first = revision()
    refs = artifacts()
    with pytest.raises(RevisionConflictError):
        repo.save_revision("contracts", first)
    repo.save_managed_revision("contracts", first, lambda: refs)
    repo.save_managed_revision("contracts", first, unexpected_call)
    with pytest.raises(RevisionConflictError):
        repo.save_managed_revision("contracts", revision(text="Changed."), unexpected_call)
    with pytest.raises(RevisionConflictError):
        repo.publish_revision("contracts", "document", "r1", expected_revision=None)

    reopened = SQLiteKnowledgeRepository(path)
    assert reopened.get_artifacts("contracts", "document", "r1") == refs
    assert reopened.get_revision("contracts", "document", "r1") == first
    assert reopened.read_published("contracts", document_ids=["document"]) == ()


def test_job_attempts_and_publication_reject_stale_workers(tmp_path):
    repo = SQLiteKnowledgeRepository(tmp_path / "knowledge.db")
    repo.create_catalog(catalog())
    job = setup_index_job(repo)
    assert repo.enqueue_index("contracts", "document", "r1") == job
    assert repo.get_index_job("another", job.id) is None
    with pytest.raises(RevisionConflictError):
        repo.publish_ready(
            "contracts", "document", "r1", expected_revision=None, verify_index=unexpected_call
        )

    first = repo.claim_index("contracts", job.id)
    assert (first.status, first.attempts) == ("running", 1)
    with pytest.raises(RevisionConflictError):
        repo.claim_index("contracts", job.id)
    repo.fail_index("contracts", job.id, 1, "interrupted")
    failed = repo.get_index_job("contracts", job.id)
    assert (failed.status, failed.error) == ("failed", "interrupted")
    second = repo.claim_index("contracts", job.id)
    assert (second.status, second.attempts, second.error) == ("running", 2, None)
    assert repo.reset_index("contracts", job.id, expected_attempt=2).status == "queued"
    third = repo.claim_index("contracts", job.id)
    with pytest.raises(RevisionConflictError):
        repo.finish_index("contracts", job.id, 2, unexpected_call)
    repo.fail_index("contracts", job.id, 2, "stale")
    assert repo.get_index_job("contracts", job.id) == third

    writes = []
    ready = repo.finish_index("contracts", job.id, 3, lambda: writes.append("indexed"))
    assert (ready.status, ready.attempts, writes) == ("succeeded", 3, ["indexed"])
    assert repo.claim_index("contracts", job.id) == ready
    repo.publish_ready(
        "contracts", "document", "r1", expected_revision=None, verify_index=lambda: None
    )
    with pytest.raises(RevisionConflictError):
        repo.reset_index("contracts", job.id, expected_attempt=3)
    with pytest.raises(RevisionConflictError):
        repo.publish_ready(
            "contracts", "document", "r1", expected_revision=None, verify_index=unexpected_call
        )
    repo.unpublish_document("contracts", "document", expected_revision="r1")
    assert repo.reset_index("contracts", job.id, expected_attempt=3).status == "queued"


def test_tombstone_blocks_old_workers_and_purge_keeps_shared_artifacts(tmp_path):
    repo = SQLiteKnowledgeRepository(tmp_path / "knowledge.db")
    repo.create_catalog(catalog())
    repo.create_catalog(catalog("other", "other-index"))
    job = setup_index_job(repo)
    repo.claim_index("contracts", job.id)
    repo.save_managed_revision("other", revision(), artifacts)

    with pytest.raises(RevisionConflictError):
        repo.mark_deleted("contracts", "document", expected_revision="stale")
    repo.mark_deleted("contracts", "document", expected_revision=None)
    repo.mark_deleted("contracts", "document", expected_revision=None)
    with pytest.raises(RevisionConflictError):
        repo.finish_index("contracts", job.id, 1, unexpected_call)
    repo.fail_index("contracts", job.id, 1, "stale after deletion")
    assert repo.get_index_job("contracts", job.id).status == "running"
    with pytest.raises(RevisionConflictError):
        repo.save_managed_revision("contracts", revision(), unexpected_call)
    with pytest.raises(RevisionConflictError):
        repo.delete_catalog("contracts", expected_version=1)

    removed_vectors = []
    repo.purge_deleted("contracts", "document", removed_vectors.extend, unexpected_call)
    assert removed_vectors == [revision().chunks.leaves[0].id]
    assert repo.get_revision("contracts", "document", "r1") is None
    assert repo.get_index_job("contracts", job.id) is None
    assert repo.get_artifacts("other", "document", "r1") == artifacts()
    assert repo.list_documents("contracts", offset=0, limit=10)[0].deleted
    repo.purge_deleted("contracts", "document", unexpected_call, unexpected_call)


def test_index_write_holds_the_lock_used_by_tombstone_and_reset(tmp_path):
    path = tmp_path / "knowledge.db"
    repo = SQLiteKnowledgeRepository(path)
    repo.create_catalog(catalog())
    job = setup_index_job(repo)
    repo.claim_index("contracts", job.id)

    def write_index():
        # Both lifecycle operations would need this second connection's write lock.
        with closing(sqlite3.connect(path, timeout=0)) as concurrent:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                concurrent.execute("BEGIN IMMEDIATE")

    repo.finish_index("contracts", job.id, 1, write_index)
    repo.mark_deleted("contracts", "document", expected_revision=None)
    with pytest.raises(RevisionConflictError):
        repo.finish_index("contracts", job.id, 1, unexpected_call)


def test_failed_purge_retains_cleanup_work_and_allows_backup_of_remaining_refs(tmp_path):
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore

    repo = SQLiteKnowledgeRepository(tmp_path / "knowledge.db")
    repo.create_catalog(catalog())
    store = LocalArtifactStore(tmp_path / "files")
    artifact_bundle = artifacts()
    for kind in ("source", "parsed", "markdown"):
        store.put(kind, b"source")
    repo.save_managed_revision("contracts", revision(), lambda: artifact_bundle)
    repo.mark_deleted("contracts", "document", expected_revision=None)

    def remove_then_fail(ref):
        store.delete(ref)
        raise OSError("interrupted")

    with pytest.raises(OSError, match="interrupted"):
        repo.purge_deleted("contracts", "document", lambda ids: None, remove_then_fail)
    with pytest.raises(RevisionConflictError):
        repo.delete_catalog("contracts", expected_version=1)

    def write_bundle(snapshot, refs):
        assert snapshot.is_file()
        assert refs == ()

    repo.backup_snapshot(write_bundle)
    repo.purge_deleted("contracts", "document", unexpected_call, store.delete)
    repo.delete_catalog("contracts", expected_version=1)
    assert repo.list_catalogs() == ()


def test_backup_snapshots_catalogs_and_restored_index_requires_rebuild(tmp_path):
    path = tmp_path / "knowledge.db"
    repo = SQLiteKnowledgeRepository(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    repo.create_catalog(catalog())
    job = setup_index_job(repo)
    repo.claim_index("contracts", job.id)
    repo.finish_index("contracts", job.id, 1, lambda: None)
    repo.publish_ready(
        "contracts", "document", "r1", expected_revision=None, verify_index=lambda: None
    )
    repo.save_revision("legacy", revision())
    repo.publish_revision("legacy", "document", "r1", expected_revision=None)
    destination = tmp_path / "restored.db"

    def write_bundle(snapshot, refs):
        with closing(sqlite3.connect(snapshot)) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        expected = artifacts()
        assert set((ref.kind, ref.sha256, ref.size) for ref in refs) == {
            (ref.kind, ref.sha256, ref.size)
            for ref in (expected.source, expected.parsed, expected.markdown)
        }
        copyfile(snapshot, destination)

    repo.backup_snapshot(write_bundle)
    restored = SQLiteKnowledgeRepository(destination)
    assert restored.get_catalog("contracts") == repo.get_catalog("contracts")
    assert restored.read_published("contracts", document_ids=["document"]) == (revision(),)
    restored.invalidate_restored_indexes()
    assert restored.read_published("contracts", document_ids=["document"]) == ()
    assert restored.read_published("legacy", document_ids=["document"]) == ()
    assert restored.get_index_job("contracts", job.id).status == "queued"
    assert restored.get_revision("contracts", "document", "r1") == revision()
    assert restored.get_revision("legacy", "document", "r1") == revision()
    assert repo.read_published("contracts", document_ids=["document"]) == (revision(),)


def test_listing_pages_and_recreated_catalog_isolate_old_jobs(tmp_path):
    repo = SQLiteKnowledgeRepository(tmp_path / "knowledge.db")
    repo.create_catalog(catalog())
    original = repo.get_catalog("contracts")
    job = setup_index_job(repo)
    repo.save_managed_revision("contracts", revision("another", "r1"), artifacts)
    repo.save_managed_revision("contracts", revision("document", "r2"), artifacts)
    entries = repo.list_documents("contracts", offset=1, limit=1)
    assert [(entry.document_id, entry.published_revision, entry.deleted) for entry in entries] == [
        ("document", None, False)
    ]
    assert repo.list_revisions("contracts", "document", offset=1, limit=1) == ("r2",)
    for doc_id in ("document", "another"):
        repo.mark_deleted("contracts", doc_id, expected_revision=None)
        repo.purge_deleted("contracts", doc_id, lambda ids: None, lambda ref: None)
    repo.delete_catalog("contracts", expected_version=1)
    repo.create_catalog(catalog())
    current = repo.get_catalog("contracts")
    assert current.generation != original.generation
    assert current.version > original.version
    with pytest.raises(RevisionConflictError):
        repo.update_catalog("contracts", "Stale", expected_version=original.version)
    with pytest.raises(RevisionConflictError):
        repo.save_managed_revision(
            "contracts", revision(), unexpected_call, expected_generation=original.generation
        )
    with pytest.raises(RevisionConflictError):
        repo.enqueue_index("contracts", "document", "r1", expected_generation=original.generation)
    assert setup_index_job(repo).id != job.id
    assert repo.get_index_job("contracts", job.id) is None


def test_job_discovery_filters_durable_states_and_pages(tmp_path):
    path = tmp_path / "knowledge.db"
    repo = SQLiteKnowledgeRepository(path)
    assert repo.workspace_identity == path.as_uri()
    repo.create_catalog(catalog())
    running = setup_index_job(repo)
    repo.claim_index("contracts", running.id)
    repo.save_managed_revision("contracts", revision("alternate"), artifacts)
    queued = repo.enqueue_index("contracts", "alternate", "r1")
    reopened = SQLiteKnowledgeRepository(path)

    assert reopened.list_index_jobs("contracts", status=None, offset=0, limit=1) == (queued,)
    assert reopened.list_index_jobs("contracts", status="running", offset=0, limit=2)[0].id == (
        running.id
    )
    assert reopened.list_index_jobs("contracts", status="queued", offset=1, limit=2) == ()
    with pytest.raises(ValueError):
        reopened.list_index_jobs("contracts", status="unknown", offset=0, limit=2)


def test_orphan_collection_preserves_all_retained_artifact_references(tmp_path):
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore

    repo = SQLiteKnowledgeRepository(tmp_path / "knowledge.db")
    repo.create_catalog(catalog())
    repo.create_catalog(catalog("other", "other-index"))
    store = LocalArtifactStore(tmp_path / "files")
    bundle = artifacts()
    retained = (
        store.put("source", b"source"),
        store.put("parsed", b"source"),
        store.put("markdown", b"source"),
    )
    orphan = store.put("source", b"abandoned")
    repo.save_managed_revision("other", revision(), lambda: bundle)
    repo.mark_deleted("other", "document", expected_revision=None)

    removed = repo.collect_orphan_artifacts(
        list_artifacts=lambda: (*retained, orphan, orphan), delete_artifact=store.delete
    )
    assert removed == 1
    for ref in retained:
        assert store.read(ref) == b"source"
    with pytest.raises(FileNotFoundError):
        store.read(orphan)
