import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_examples import invoke, successful, write_json


@contextmanager
def model_endpoint(data, *, status=200):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            if status != 200:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
                return
            if self.path.endswith("/embeddings"):
                response = {
                    "object": "list",
                    "model": body["model"],
                    "data": [
                        {"object": "embedding", "index": index, "embedding": [1.0, 0.2, 0.1]}
                        for index in reversed(range(len(body["input"])))
                    ],
                    "usage": {"prompt_tokens": 10, "total_tokens": 10},
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
                return
            response = {
                "id": "test-response",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "usage": {"prompt_tokens": 14, "completion_tokens": 8, "total_tokens": 22},
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "generate_structured_output",
                                        "arguments": json.dumps(data),
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def settings(tmp_path, monkeypatch, url):
    monkeypatch.setenv("EXAMPLE_MODEL_URL", url)
    monkeypatch.setenv("EXAMPLE_MODEL_KEY", "secret-for-local-test")
    monkeypatch.setenv("EXAMPLE_DEFAULT_MODEL", "default-test-model")
    monkeypatch.setenv("EXAMPLE_ROUTE_MODEL", "route-test-model")
    common = {"api_key_env": "EXAMPLE_MODEL_KEY", "base_url_env": "EXAMPLE_MODEL_URL"}
    return write_json(
        tmp_path / "models.json",
        {
            "default_alias": "default",
            "task_routes": {"structured_extraction": "route", "document_extraction": "route"},
            "models": {
                "default": {**common, "model_env": "EXAMPLE_DEFAULT_MODEL"},
                "route": {**common, "model_env": "EXAMPLE_ROUTE_MODEL"},
            },
            "execution": {"timeout_seconds": 10, "max_attempts": 1},
        },
    )


def test_real_native_model_transport_routing_validation_and_logs(tmp_path, monkeypatch):
    pytest.importorskip("agentscope")
    source = tmp_path / "contract.txt"
    source.write_text("PRIVATE_INPUT_SENTINEL 合同金额3200元。")
    schema = write_json(
        tmp_path / "schema.json",
        {"type": "object", "properties": {"amount": {"type": "integer"}}, "required": ["amount"]},
    )
    with model_endpoint({"amount": 3200}) as (url, calls):
        config = settings(tmp_path, monkeypatch, url)
        args = [source, "--config", config, "--schema", schema]
        result = invoke("models", "extract", *args)
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert parsed["data"] == {"amount": 3200}
        assert parsed["input_ids"] == ["contract.txt"]
        assert calls[0]["model"] == "route-test-model"
        assert "PRIVATE_INPUT_SENTINEL" in str(calls[0])
        assert "PRIVATE_INPUT_SENTINEL" not in result.stderr
        assert "secret-for-local-test" not in result.stderr + result.stdout
        assert "model.completed" in result.stderr
        successful("models", "extract", *args, "--model-alias", "default")
        assert len(calls) == 2 and calls[-1]["model"] == "default-test-model"
    with model_endpoint({"amount": "invalid"}) as (url, calls):
        config = settings(tmp_path, monkeypatch, url)
        result = invoke("models", "extract", source, "--config", config, "--schema", schema)
        assert result.returncode == 2
        assert not result.stdout
        assert "StructuredOutputError" in result.stderr
        # 原生框架先验证，失败走 forced/auto/none 三种兼容策略。
        # 失败数据不会返回到 SDK Extractor；这与后端已返回数据的校验失败不同。
        assert len(calls) == 3


def test_langextract_provider_uses_thread_and_task_route(tmp_path, monkeypatch):
    pytest.importorskip("agentscope")
    pytest.importorskip("langextract")
    prompts = write_json(tmp_path / "prompts.json", ["从原文提取合同金额"])
    schema = write_json(
        tmp_path / "envelope.json",
        {
            "type": "object",
            "properties": {"extractions": {"type": "array"}},
            "required": ["extractions"],
        },
    )
    with model_endpoint({"extractions": []}) as (url, calls):
        config = settings(tmp_path, monkeypatch, url)
        result = successful(
            "models", "langextract", prompts, "--config", config, "--schema", schema
        )
        assert result == [[{"extractions": []}]]
        assert calls[0]["model"] == "route-test-model"


def test_provider_error_code_cannot_leak_remote_payload(tmp_path, monkeypatch):
    pytest.importorskip("agentscope")
    source = tmp_path / "contract.txt"
    source.write_text("PRIVATE_INPUT_SENTINEL 合同金额3200元。")
    schema = write_json(tmp_path / "schema.json", {"type": "object"})
    with model_endpoint(
        {
            "error": {
                "message": "PRIVATE_PROVIDER_MESSAGE_SENTINEL",
                "type": "invalid_request_error",
                "code": "PRIVATE_PROVIDER_CODE_SENTINEL",
            }
        },
        status=400,
    ) as (url, calls):
        config = settings(tmp_path, monkeypatch, url)
        result = invoke("models", "extract", source, "--config", config, "--schema", schema)
    assert result.returncode == 2 and calls
    assert "BadRequestError" in result.stderr
    assert "PRIVATE_" not in result.stdout + result.stderr
    assert "secret-for-local-test" not in result.stdout + result.stderr
    assert json.loads(result.stderr.splitlines()[-1])["code"] == "example_error"


def test_native_permission_skills_and_storage_are_independent(tmp_path):
    pytest.importorskip("agentscope")
    assert successful("runtime", "permission")["status"] == "allow"
    assert successful("runtime", "permission", "--deny")["status"] == "deny"
    skill = tmp_path / "skills" / "contract-review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: contract-review\ndescription: Review a contract\n---\nRead references.\n"
    )
    resources = successful("runtime", "skills", skill.parent)
    assert resources[0]["id"] == "contract-review"
    pytest.importorskip("sqlalchemy")
    assert (
        successful(
            "runtime",
            "storage",
            "--db",
            tmp_path / "native.db",
            "--user-id",
            "tenant-a",
            "--create-tables",
        )
        == []
    )


def test_tcp_probe_real_connection_and_closed_port(tmp_path):
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        result = successful("execution", "--host", "127.0.0.1", "--port", port, "--probes", 2)
        assert len(result) == 2 and all(item["status"] == "reachable" for item in result)
    failed = invoke("execution", "--host", "127.0.0.1", "--port", port, "--max-attempts", 2)
    assert failed.returncode == 2
    assert "RetryableError" in failed.stderr


def test_context_snapshot_restore_does_not_request_model(tmp_path, monkeypatch):
    pytest.importorskip("agentscope")
    with model_endpoint({}) as (url, calls):
        config = settings(tmp_path, monkeypatch, url)
        saved = tmp_path / "context.json"
        result = invoke(
            "runtime", "context", "--config", config, "--model-alias", "default", "--output", saved
        )
        assert result.returncode == 0, result.stderr
        restored = successful(
            "runtime", "context", "--config", config, "--model-alias", "default", "--state", saved
        )
        original = json.loads(saved.read_text())
        assert restored["backend"] == "agentscope"
        assert restored["payload"]["session_id"] == original["payload"]["session_id"]
        assert calls == []


def test_docling_markdown_structure_and_native_pdf_with_empty_offline_cache(tmp_path, monkeypatch):
    pytest.importorskip("docling")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "empty-hf-cache"))
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    source = tmp_path / "contract.md"
    source.write_text("# 合同\n\n付款32000元。\n\n## 保密\n\n不得披露。")
    parsed = successful("documents", "parse", source)
    assert sum(e["kind"] == "heading" for e in parsed["document"]["elements"]) == 2
    # 构造含可选取文本的合法 PDF。只生成测试输入，不使用模型或网络。
    content = b"BT /F1 12 Tf 20 100 Td (Contract amount 32000 CNY.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    position = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{position}\n%%EOF\n".encode()
    )
    path = tmp_path / "text.pdf"
    path.write_bytes(pdf)
    result = successful("documents", "native-pdf", path)
    assert "32000" in result["document"]["text"]
    assert not any(e["kind"] in {"heading", "table"} for e in result["document"]["elements"])
    assert not (tmp_path / "empty-hf-cache" / "hub").exists()


@pytest.mark.parametrize("strategy", ["recursive", "delimiter", "sentence", "token"])
def test_chunk_strategies_use_real_local_tokenizer_when_needed(tmp_path, monkeypatch, strategy):
    pytest.importorskip("chonkie")
    source = tmp_path / "terms.txt"
    source.write_text("Payment is due in 30 days. Confidentiality lasts 2 years.")
    policy = write_json(
        tmp_path / "policy.json",
        {"strategy": strategy, "size": 6 if strategy == "token" else 30, "preserve_tables": False},
    )
    args = ["chunk", source, "--policy", policy]
    if strategy == "token":
        tokenizers = pytest.importorskip("tokenizers")
        from tokenizers.models import WordLevel
        from tokenizers.pre_tokenizers import Whitespace

        tokenizer = tokenizers.Tokenizer(WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
        tokenizer.pre_tokenizer = Whitespace()
        path = tmp_path / "tokenizer.json"
        tokenizer.save(str(path))
        monkeypatch.setenv("TOKENIZER_PATH", str(path))
        args += [
            "--tokenizer-factory",
            "examples.providers:local_tokenizer",
            "--tokenizer-id",
            "fixture-wordlevel-v1",
        ]
    result = successful("documents", *args)
    assert "".join(c["text"] for c in result["chunks"]) == source.read_text()


def test_mmr_and_dedup_use_actual_selection_component():
    from test_examples import ROOT

    fixture = ROOT / "examples" / "data"
    selected = successful(
        "retrieval",
        "select",
        fixture / "candidates-fixture.json",
        "--method",
        "mmr",
        "--query",
        fixture / "query-fixture.json",
        "--limit",
        1,
    )
    assert [row["chunk"]["id"] for row in selected["selected"]] == ["fixture-payment"]
    unique = successful(
        "retrieval", "select", fixture / "candidates-fixture.json", "--method", "dedup"
    )
    assert len(unique["selected"]) == 2


def test_semantic_chunking_calls_compatible_embedding_endpoint(tmp_path, monkeypatch):
    pytest.importorskip("chonkie")
    pytest.importorskip("openai")
    source = tmp_path / "source.txt"
    source.write_text(
        "Payment is due in 30 days. The total amount is 32000 CNY. "
        "Acceptance is required in 15 days. Confidentiality lasts two years. "
        "The buyer owns the delivered files. Support is included for one year."
    )
    policy = write_json(
        tmp_path / "policy.json",
        {"strategy": "semantic", "size": 100, "embedding_batch_size": 2, "similarity_window": 2},
    )
    with model_endpoint({}) as (url, calls):
        for key, value in {
            "EMBEDDING_MODEL": "embedding-fixture",
            "EMBEDDING_BASE_URL": url,
            "EMBEDDING_API_KEY": "local-test-key",
            "EMBEDDING_DIMENSION": "3",
        }.items():
            monkeypatch.setenv(key, value)
        result = successful(
            "documents",
            "chunk",
            source,
            "--policy",
            policy,
            "--embedding-factory",
            "examples.providers:compatible_embeddings",
            "--embedding-id",
            "fixture-embedding-v1-d3",
        )
    assert calls and all(len(call["input"]) <= 2 for call in calls)
    assert "".join(row["text"] for row in result["chunks"]) == source.read_text()


def test_retrieval_metrics_use_real_ir_measures():
    pytest.importorskip("ir_measures")
    from test_examples import ROOT

    fixture = ROOT / "examples" / "data"
    scores = successful(
        "evaluate",
        "retrieval",
        fixture / "ranking.json",
        "--labels",
        fixture / "labels.json",
        "--k",
        1,
    )
    assert all(score == 1.0 for score in scores.values())
