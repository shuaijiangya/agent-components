import sqlite3
from contextlib import closing
from zipfile import ZipFile

import pytest


def test_artifacts_are_content_addressed_across_store_instances(tmp_path):
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore

    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root)
    ref = store.put("source", b"abc")

    assert ref.kind == "source"
    assert ref.sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert ref.size == 3
    assert store.put("source", b"abc") == ref

    markdown = store.put("markdown", b"abc")
    reopened = LocalArtifactStore(root)
    assert reopened.read(ref) == b"abc"
    reopened.delete(ref)
    reopened.delete(ref)
    with pytest.raises(FileNotFoundError):
        reopened.read(ref)
    assert reopened.read(markdown) == b"abc"


def test_artifact_read_rejects_same_size_corruption(tmp_path):
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore

    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root)
    ref = store.put("parsed", b"abc")
    blob_path = next(path for path in root.rglob("*") if path.is_file())
    blob_path.write_bytes(b"xyz")

    with pytest.raises(ValueError):
        store.read(ref)


@pytest.mark.parametrize("changes", [{"sha256": "../../secret"}, {"kind": "../secret"}])
def test_artifact_store_revalidates_copied_references(tmp_path, changes):
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore

    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = store.put("source", b"abc")
    unsafe = ref.model_copy(update=changes)

    with pytest.raises(ValueError):
        store.read(unsafe)
    with pytest.raises(ValueError):
        store.delete(unsafe)
    assert store.read(ref) == b"abc"


def make_backup(tmp_path):
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore
    from agent_components.knowledge_base.backup import write_backup

    database = tmp_path / "snapshot.sqlite"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TABLE example (value TEXT NOT NULL)")
        db.execute("INSERT INTO example VALUES ('restored content')")
    store = LocalArtifactStore(tmp_path / "source-artifacts")
    ref = store.put("source", b"original bytes")
    archive = tmp_path / "backup.zip"
    write_backup(database, [ref, ref], store, archive)
    return archive, ref, database


def test_backup_contains_database_and_artifact_bytes(tmp_path):
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore
    from agent_components.knowledge_base.backup import restore_backup

    archive, ref, snapshot = make_backup(tmp_path)
    LocalArtifactStore(tmp_path / "source-artifacts").delete(ref)
    snapshot.unlink()
    restored_db = tmp_path / "restored" / "knowledge.sqlite"
    restored_store = LocalArtifactStore(tmp_path / "restored" / "artifacts")

    restore_backup(archive, restored_db, restored_store)

    with closing(sqlite3.connect(restored_db)) as db:
        assert db.execute("SELECT value FROM example").fetchall() == [("restored content",)]
    assert restored_store.read(ref) == b"original bytes"


def test_backup_restore_refuses_an_existing_database(tmp_path):
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore
    from agent_components.knowledge_base.backup import restore_backup

    archive, ref, _ = make_backup(tmp_path)
    restored_db = tmp_path / "existing.sqlite"
    restored_db.write_bytes(b"existing database")
    restored_store = LocalArtifactStore(tmp_path / "restored-artifacts")

    with pytest.raises(FileExistsError):
        restore_backup(archive, restored_db, restored_store)

    assert restored_db.read_bytes() == b"existing database"
    with pytest.raises(FileNotFoundError):
        restored_store.read(ref)


def test_backup_refuses_to_replace_an_existing_file(tmp_path):
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore
    from agent_components.knowledge_base.backup import write_backup

    _, ref, snapshot = make_backup(tmp_path)
    original = snapshot.read_bytes()
    store = LocalArtifactStore(tmp_path / "source-artifacts")
    with pytest.raises(FileExistsError):
        write_backup(snapshot, [ref], store, snapshot)
    assert snapshot.read_bytes() == original


def test_artifact_store_rejects_shared_ownership_across_workspaces(tmp_path):
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore

    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root)
    store.bind_workspace("first")
    ref = store.put("source", b"original bytes")
    same_workspace = LocalArtifactStore(root)
    same_workspace.bind_workspace("first")
    assert same_workspace.read(ref) == b"original bytes"
    with pytest.raises(ValueError, match="workspace"):
        LocalArtifactStore(root).bind_workspace("other")


@pytest.mark.parametrize("alteration", ["artifact", "database", "unexpected path"])
def test_invalid_backup_publishes_no_database_or_artifacts(tmp_path, alteration):
    from agent_components.knowledge_base.adapters.files import LocalArtifactStore
    from agent_components.knowledge_base.backup import restore_backup

    archive, ref, snapshot = make_backup(tmp_path)
    invalid_archive = tmp_path / "tampered.zip"
    with ZipFile(archive) as source, ZipFile(invalid_archive, "w") as invalid:
        for name in source.namelist():
            payload = source.read(name)
            if alteration == "artifact" and payload == b"original bytes":
                payload = b"tampered bytes"
            if alteration == "database" and payload == snapshot.read_bytes():
                payload = payload[:-1] + bytes([payload[-1] ^ 1])
            invalid.writestr(name, payload)
        if alteration == "unexpected path":
            invalid.writestr("../escaped", b"unexpected member")
    restored_db = tmp_path / "restored" / "knowledge.sqlite"
    restored_store = LocalArtifactStore(tmp_path / "restored-artifacts")

    with pytest.raises(ValueError):
        restore_backup(invalid_archive, restored_db, restored_store)

    assert not restored_db.exists()
    assert not (tmp_path / "escaped").exists()
    with pytest.raises(FileNotFoundError):
        restored_store.read(ref)
