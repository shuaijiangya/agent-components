import pytest

from agent_components.chunking import ChunkPolicy, SectionOptions
from agent_components.core.errors import RevisionConflictError
from agent_components.parsing import DoclingParser
from agent_components.retrieval import LanceDBIndex, RetrievalOptions


class Embedding:
    dimension = 2

    def __init__(self):
        self.fail = False
        self.on_request = None

    def embed_batch(self, texts):
        if self.fail:
            raise RuntimeError("test provider unavailable")
        if self.on_request is not None:
            self.on_request()
        return [(1.0, 0.0)] * len(texts)


@pytest.fixture
def workspace(tmp_path):
    pytest.importorskip("lancedb")
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore
    from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository
    from agent_components.knowledge_base.lifecycle import Catalog
    from agent_components.knowledge_base.managed import ManagedKnowledgeBase

    repo = SQLiteKnowledgeRepository(tmp_path / "content.db")
    repo.create_catalog(Catalog("contracts", "采购合同", "contracts-index", "embedding-v1"))
    index = LanceDBIndex(tmp_path / "index", dimensions=2)
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    embedding = Embedding()
    knowledge = ManagedKnowledgeBase(
        "contracts",
        repo,
        artifacts,
        index,
        embedding,
        index_id="contracts-index",
        embedding_id="embedding-v1",
    )
    return knowledge, repo, index, artifacts, embedding


def _ingest(knowledge, path, text, document_id="contract"):
    path.write_text(text, encoding="utf-8")
    parsed = DoclingParser().parse(path, document_id=document_id)
    return knowledge.save_parsed(
        parsed, source=path.read_bytes(), policy=ChunkPolicy(strategy="character", size=24)
    )


@pytest.mark.integration
def test_publication_checks_job_and_real_index_and_failed_retry_keeps_old_version(
    workspace, tmp_path
):
    kb, repo, index, _, embedding = workspace
    old = _ingest(kb, tmp_path / "a.txt", "采购尾款在验收后支付。")
    old_id = old.document.revision_id
    with pytest.raises(RevisionConflictError):
        kb.publish_revision("contract", old_id, expected_revision=None)
    assert kb.published_chunks(document_ids=["contract"]) == ()
    first_job = kb.enqueue_index("contract", old_id)
    assert kb.enqueue_index("contract", old_id).id == first_job.id
    assert kb.run_index(first_job.id).status == "succeeded"
    kb.publish_revision("contract", old_id, expected_revision=None)
    new = _ingest(kb, tmp_path / "a.txt", "采购尾款在第二次验收后支付。")
    new_id = new.document.revision_id
    job = kb.enqueue_index("contract", new_id)
    embedding.fail = True
    with pytest.raises(RuntimeError, match="unavailable"):
        kb.run_index(job.id)
    current = repo.get_index_job("contracts", job.id)
    assert current.status == "failed" and current.attempts == 1
    assert {chunk.revision_id for chunk in kb.published_chunks(document_ids=["contract"])} == {
        old_id
    }
    embedding.fail = False
    succeeded = kb.run_index(job.id)
    assert succeeded.status == "succeeded" and succeeded.attempts == 2
    index.delete_records(chunk_ids=[new.chunks.leaves[0].id])
    with pytest.raises(RevisionConflictError):
        kb.publish_revision("contract", new_id, expected_revision=old_id)
    repo.reset_index("contracts", job.id, expected_attempt=2)
    kb.run_index(job.id)
    kb.publish_revision("contract", new_id, expected_revision=old_id)
    result = kb.search(
        "尾款",
        authorized_document_ids=lambda: ["contract"],
        options=RetrievalOptions(mode="hybrid", selection="exact"),
    )
    assert [match.revision_id for match in result.matches] == [new_id]


@pytest.mark.integration
@pytest.mark.parametrize("revoke", ["permission", "delete", "publish"])
def test_managed_search_checks_fresh_authority_before_returning_hits(workspace, tmp_path, revoke):
    kb, _, _, _, embedding = workspace
    version = _ingest(kb, tmp_path / "a.txt", "采购尾款在验收后支付。")
    revision_id = version.document.revision_id
    kb.run_index(kb.enqueue_index("contract", revision_id).id)
    kb.publish_revision("contract", revision_id, expected_revision=None)
    grants = ["contract"]
    if revoke == "publish":
        new = _ingest(kb, tmp_path / "a.txt", "采购尾款提前支付。")
        new_id = new.document.revision_id
        kb.run_index(kb.enqueue_index("contract", new_id).id)
        embedding.on_request = lambda: kb.publish_revision(
            "contract", new_id, expected_revision=revision_id
        )
    elif revoke == "delete":
        embedding.on_request = lambda: kb.delete_document("contract", expected_revision=revision_id)
    else:
        embedding.on_request = grants.clear
    with pytest.raises(RevisionConflictError, match="visibility"):
        kb.search("尾款", authorized_document_ids=lambda: grants.copy())


@pytest.mark.integration
def test_managed_does_not_return_parent_containing_hidden_leaf(workspace, tmp_path):
    kb, _, _, _, _ = workspace
    path = tmp_path / "policy.txt"
    raw = "采购流程说明。机密补充条款。"
    path.write_text(raw, encoding="utf-8")
    version = kb.save_parsed(
        DoclingParser().parse(path, document_id="policy"),
        source=raw.encode(),
        policy=ChunkPolicy(strategy="character", size=7),
        section_options=SectionOptions(max_parent_chars=42),
    )
    revision = version.document.revision_id
    kb.run_index(kb.enqueue_index("policy", revision).id)
    kb.publish_revision("policy", revision, expected_revision=None)
    public = version.chunks.leaves[0]
    options = RetrievalOptions(mode="keyword", parent_mode="replace")
    unfiltered = kb.search("采购", authorized_document_ids=lambda: ["policy"], options=options)
    assert raw in unfiltered.context.windows[0].text

    filtered = kb.search(
        "采购",
        authorized_document_ids=lambda: ["policy"],
        allowed_chunk=lambda chunk: chunk.kind == "parent" or chunk.id == public.id,
        options=options,
    )
    assert filtered.context.selected_ids == (public.id,)
    assert filtered.context.windows[0].text == public.text


@pytest.mark.integration
def test_stale_ingest_cannot_write_into_recreated_catalog(workspace, tmp_path, monkeypatch):
    from agent_components.chunking import Rechunker
    from agent_components.knowledge_base import Catalog

    kb, repo, _, _, _ = workspace
    original = Rechunker.rechunk

    def replace_catalog(rechunker, *args, **kwargs):
        repo.delete_catalog("contracts", expected_version=1)
        repo.create_catalog(Catalog("contracts", "新的目录", "other-index", "other-model"))
        return original(rechunker, *args, **kwargs)

    monkeypatch.setattr(Rechunker, "rechunk", replace_catalog)
    with pytest.raises(RevisionConflictError, match="generation"):
        _ingest(kb, tmp_path / "replacement.txt", "旧工作者遗留的待写入内容")
    assert repo.list_documents("contracts", offset=0, limit=10) == ()


@pytest.mark.integration
def test_abandoned_upload_artifacts_can_be_collected_without_removing_live_references(
    workspace, tmp_path, monkeypatch
):
    kb, _, _, artifacts, _ = workspace
    live = _ingest(kb, tmp_path / "live.txt", "已持久化的文档")
    old_job = kb.enqueue_index("contract", live.document.revision_id)
    assert kb.list_index_jobs(status="queued") == (old_job,)
    assert kb.list_index_jobs(status="running") == ()
    put = artifacts.put

    def fail_before_parsed(kind, data):
        if kind == "parsed":
            raise OSError("test disk write failure")
        return put(kind, data)

    monkeypatch.setattr(artifacts, "put", fail_before_parsed)
    with pytest.raises(OSError, match="test disk"):
        _ingest(kb, tmp_path / "orphan.txt", "事务尚未保存的文档", "other")
    assert len(artifacts.list_refs()) == 4
    assert kb.collect_orphan_artifacts() == 1
    assert kb.collect_orphan_artifacts() == 0
    assert len(artifacts.list_refs()) == 3
    assert kb.read_artifact("contract", live.document.revision_id, "source").decode() == (
        "已持久化的文档"
    )


@pytest.mark.integration
def test_parsed_artifacts_rechunk_backup_restore_and_shared_delete(workspace, tmp_path):
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore
    from agent_components.knowledge_base.adapters.sqlite import SQLiteKnowledgeRepository
    from agent_components.knowledge_base.managed import ManagedKnowledgeBase, restore_workspace

    kb, repo, index, artifacts, embedding = workspace
    file = tmp_path / "a.txt"
    original = "设备验收通过后支付尾款。" * 6
    old = _ingest(kb, file, original)
    sibling = _ingest(kb, file, original, "sibling")
    file.unlink()
    newer = kb.rechunk_revision(
        "contract",
        old.document.revision_id,
        ChunkPolicy(strategy="character", size=14),
        section_options=SectionOptions(max_parent_chars=42),
    )
    assert newer.trace.previous_revision_id == old.document.revision_id
    assert len(kb.chunk_page("contract", newer.document.revision_id, offset=1, limit=2)) == 2
    assert len(kb.list_revisions("contract", offset=0, limit=8)) == 2
    assert kb.read_artifact("contract", newer.document.revision_id, "source") == original.encode()
    parsed = kb.read_artifact("contract", newer.document.revision_id, "parsed")
    assert original.encode() in parsed
    assert kb.read_artifact("contract", newer.document.revision_id, "markdown") == original.encode()
    kb.run_index(kb.enqueue_index("contract", newer.document.revision_id).id)
    kb.publish_revision("contract", newer.document.revision_id, expected_revision=None)
    backup = tmp_path / "warehouse.zip"
    kb.backup_workspace(backup)
    restored_path = tmp_path / "restored.db"
    with pytest.raises(ValueError, match="workspace"):
        restore_workspace(backup, restored_path, artifacts)
    assert not restored_path.exists()
    recovered_files = LocalArtifactStore(tmp_path / "restored-files")
    plan = restore_workspace(backup, restored_path, recovered_files)
    assert [(p.knowledge_base_id, p.document_id, p.revision_id) for p in plan] == [
        ("contracts", "contract", newer.document.revision_id)
    ]
    recovered_index = LanceDBIndex(tmp_path / "restored-index", dimensions=2)
    restored = ManagedKnowledgeBase(
        "contracts",
        SQLiteKnowledgeRepository(restored_path),
        recovered_files,
        recovered_index,
        embedding,
        index_id="contracts-index",
        embedding_id="embedding-v1",
    )
    assert restored.published_chunks(document_ids=["contract"]) == ()
    recovered = plan[0]
    restored.run_index(restored.enqueue_index(recovered.document_id, recovered.revision_id).id)
    restored.publish_revision(recovered.document_id, recovered.revision_id, expected_revision=None)
    assert restored.search("验收", authorized_document_ids=lambda: ["contract"]).matches
    assert restored.read_artifact("contract", recovered.revision_id, "source") == original.encode()

    source_ref = repo.get_artifacts("contracts", "contract", newer.document.revision_id).source
    kb.delete_document("contract", expected_revision=newer.document.revision_id)
    kb.purge_deleted("contract")
    assert not index.get_records(chunk_ids=[chunk.id for chunk in newer.chunks.leaves])
    assert kb.get_revision("contract", newer.document.revision_id) is None
    assert kb.read_artifact("sibling", sibling.document.revision_id, "source") == original.encode()
    kb.delete_document("sibling", expected_revision=None)
    kb.purge_deleted("sibling")
    with pytest.raises(FileNotFoundError):
        artifacts.read(source_ref)
