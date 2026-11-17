import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def invoke(module, *args):
    assert (ROOT / "examples" / f"{module}.py").exists(), "example command not implemented"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)])}
    return subprocess.run(
        [sys.executable, "-m", f"examples.{module}", *map(str, args)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def successful(module, *args):
    result = invoke(module, *args)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_text_parse_to_chunk_preserves_source_and_rejects_overwrite(tmp_path):
    source = tmp_path / "contract.txt"
    source.write_bytes("第一章\r\n金额3200元。\r\n第二章\r\n30日内支付。".encode())
    parsed = tmp_path / "parsed.json"
    assert invoke("documents", "parse", source, "--output", parsed).returncode == 0
    policy = write_json(tmp_path / "policy.json", {"strategy": "character", "size": 12})
    chunks = successful("documents", "chunk", parsed, "--input-kind", "parsed", "--policy", policy)
    text = source.read_bytes().decode()
    assert "".join(c["text"] for c in chunks["chunks"]) == text
    assert all(text[c["start"] : c["end"]] == c["text"] for c in chunks["chunks"])
    before = parsed.read_bytes()
    result = invoke(
        "documents",
        "chunk",
        parsed,
        "--input-kind",
        "parsed",
        "--policy",
        policy,
        "--output",
        parsed,
    )
    assert result.returncode != 0
    assert parsed.read_bytes() == before


def test_parent_ids_context_and_incremental_jsonl(tmp_path):
    source = tmp_path / "contract.md"
    source.write_text(
        "# 付款\n首付款3200元。尾款验收后支付。\n# 期限\n30日内验收。", encoding="utf-8"
    )
    document = write_json(
        tmp_path / "document.json",
        {
            "id": "contract-1",
            "text": source.read_text(),
            "elements": [
                {"id": "h1", "kind": "heading", "start": 0, "end": 4, "level": 1, "title": "付款"},
                {
                    "id": "h2",
                    "kind": "heading",
                    "start": source.read_text().index("# 期限"),
                    "end": source.read_text().index("# 期限") + 4,
                    "level": 1,
                    "title": "期限",
                },
            ],
        },
    )
    policy = write_json(tmp_path / "policy.json", {"strategy": "character", "size": 8})
    corpus_path = tmp_path / "corpus.json"
    result = invoke(
        "documents",
        "parents",
        document,
        "--input-kind",
        "document",
        "--policy",
        policy,
        "--output",
        corpus_path,
    )
    assert result.returncode == 0, result.stderr
    corpus = json.loads(corpus_path.read_text())
    parents = {c["id"]: c for c in corpus["chunks"] if c["kind"] == "parent"}
    leaves = [c for c in corpus["chunks"] if c["kind"] == "leaf"]
    assert len(parents) == 2
    assert all(c["parent_id"] in parents for c in leaves)
    hits = write_json(tmp_path / "hits.json", [leaves[0]["id"]])
    expanded = successful(
        "retrieval", "context", corpus_path, "--hits", hits, "--parent-mode", "replace"
    )
    assert expanded["windows"][0]["text"] == parents[leaves[0]["parent_id"]]["text"]
    streamed = invoke("documents", "stream", source, "--policy", policy, "--window-chars", 16)
    assert streamed.returncode == 0, streamed.stderr
    rows = [json.loads(line) for line in streamed.stdout.splitlines()]
    assert "".join(c["text"] for c in rows) == source.read_text()


@pytest.mark.parametrize(
    "module", ["documents", "retrieval", "state", "models", "runtime", "execution", "evaluate"]
)
def test_component_command_help(module):
    assert invoke(module, "--help").returncode == 0


def test_schema_evaluation_reports_invalid_result(tmp_path):
    data = write_json(tmp_path / "data.json", {"amount": "3200"})
    schema = write_json(
        tmp_path / "schema.json",
        {
            "type": "object",
            "properties": {"amount": {"type": "integer"}},
            "required": ["amount"],
        },
    )
    result = invoke("evaluate", "schema", data, "--schema", schema)
    assert result.returncode == 1
    assert json.loads(result.stdout)["passed"] is False


def test_state_requires_explicit_compare_and_swap(tmp_path):
    pytest.importorskip("sqlalchemy")
    state = write_json(
        tmp_path / "state.json",
        {"backend": "application", "schema_version": "1", "payload": {"status": "proposed"}},
    )
    args = ["--db", tmp_path / "state.db", "--namespace", "tenant-a", "--key", "proposal-1"]
    first = successful(
        "state", "save", *args, "--state", state, "--expected-revision", 0, "--create-tables"
    )
    assert first["revision"] == 1
    result = invoke("state", "save", *args, "--state", state, "--expected-revision", 0)
    assert result.returncode != 0
    assert "RevisionConflictError" in result.stderr
    assert successful("state", "load", *args)["revision"] == 1


def test_lancedb_upsert_and_empty_permission_filter(tmp_path):
    pytest.importorskip("lancedb")
    records = tmp_path / "records.jsonl"
    records.write_text(
        json.dumps(
            {"id": "c1", "document_id": "d1", "revision_id": "1", "text": "付款", "vector": [1, 0]}
        )
        + "\n"
    )
    query = write_json(tmp_path / "query.json", [1, 0])
    args = ["--db", tmp_path / "index", "--dimensions", 2]
    successful("retrieval", "index", *args, "--records", records)
    successful("retrieval", "index", *args, "--records", records)
    hits = successful("retrieval", "search", *args, "--query", query)
    assert len(hits) == 1 and hits[0]["id"] == "c1"
    allowed = write_json(tmp_path / "allowed.json", [])
    assert (
        successful("retrieval", "search", *args, "--query", query, "--document-ids", allowed) == []
    )


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sqlite_sidecars_cannot_overwrite_state_input_or_output(tmp_path, suffix):
    pytest.importorskip("sqlalchemy")
    db = tmp_path / "state.db"
    sidecar = write_json(
        Path(str(db) + suffix),
        {
            "backend": "application",
            "schema_version": "1",
            "payload": {"status": "proposed"},
        },
    )
    before = sidecar.read_bytes()
    args = ["--db", db, "--namespace", "tenant-a", "--key", "proposal-1", "--create-tables"]
    result = invoke("state", "save", *args, "--state", sidecar, "--expected-revision", 0)
    assert result.returncode == 2
    assert sidecar.exists() and sidecar.read_bytes() == before
    assert not db.exists()
    result = invoke("state", "load", *args, "--output", sidecar)
    assert result.returncode == 2
    assert sidecar.exists() and sidecar.read_bytes() == before
    assert not db.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_native_storage_json_output_cannot_be_database_sidecar(tmp_path, suffix):
    db = tmp_path / "native.db"
    output = Path(str(db) + suffix)
    output.write_bytes(b"keep-this-file")
    result = invoke(
        "runtime",
        "storage",
        "--db",
        db,
        "--user-id",
        "tenant-a",
        "--create-tables",
        "--output",
        output,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["error_type"] == "ValueError"
    assert output.read_bytes() == b"keep-this-file" and not db.exists()


@pytest.mark.parametrize("link_kind", ["hard", "symbolic"])
def test_sqlite_path_guard_follows_link_aliases(tmp_path, link_kind):
    db = tmp_path / "native.db"
    sidecar = Path(str(db) + "-wal")
    sidecar.write_bytes(b"keep-this-file")
    alias = tmp_path / "output.json"
    if link_kind == "hard":
        os.link(sidecar, alias)
    else:
        alias.symlink_to(sidecar)
    result = invoke(
        "runtime",
        "storage",
        "--db",
        db,
        "--user-id",
        "tenant-a",
        "--create-tables",
        "--output",
        alias,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["error_type"] == "ValueError"
    assert sidecar.read_bytes() == alias.read_bytes() == b"keep-this-file"
    assert not db.exists()
