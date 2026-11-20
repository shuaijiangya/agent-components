import json
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest
from pydantic import SecretStr

from agent_components.core.errors import ComponentError
from agent_components.retrieval.reranking import (
    HTTPReranker,
    RerankerError,
    RerankerProvider,
    RerankScore,
)


@contextmanager
def _serve_reranker(
    body: bytes = b'{"results": [{"index": 0, "relevance_score": 0.25}]}',
    *,
    status: int = 200,
    hold_response: bool = False,
    disconnect: bool = False,
) -> Iterator[tuple[str, list[tuple[str, str | None, str | None, object]]]]:
    requests = []
    release_response = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request_body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(
                (
                    self.path,
                    self.headers.get("Authorization"),
                    self.headers.get("Content-Type"),
                    json.loads(request_body),
                )
            )
            if disconnect:
                return
            if hold_response:
                release_response.wait(timeout=5)
            with suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if status == 307:
                    self.send_header("Location", "/redirected")
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, format, *args) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v2/rerank?tenant=local", requests
        finally:
            release_response.set()
            server.shutdown()
            thread.join()


def test_http_reranker_posts_all_candidates_using_cohere_contract():
    response = (
        b'{"id":"request-1","results":[{"index":1,"relevance_score":0.9},'
        b'{"index":0,"relevance_score":0.25}]}'
    )
    with _serve_reranker(response) as (url, requests):
        provider: RerankerProvider = HTTPReranker(
            endpoint_url=url, model="example-model", api_key=SecretStr("local-key")
        )
        assert provider.rerank("查询", ["第一项", "第二项"]) == (
            RerankScore(index=0, score=0.25),
            RerankScore(index=1, score=0.9),
        )

    assert requests == [
        (
            "/v2/rerank?tenant=local",
            "Bearer local-key",
            "application/json",
            {
                "model": "example-model",
                "query": "查询",
                "documents": ["第一项", "第二项"],
                "top_n": 2,
            },
        )
    ]


def test_http_reranker_reads_bearer_token_from_environment(monkeypatch):
    with _serve_reranker() as (url, requests):
        provider = HTTPReranker(endpoint_url=url, model="test", api_key_env="TEST_RERANKER_TOKEN")
        monkeypatch.setenv("TEST_RERANKER_TOKEN", "rotated-key")
        provider.rerank("query", ("document",))

    assert requests[0][1] == "Bearer rotated-key"


def test_http_reranker_supports_endpoint_without_authentication():
    with _serve_reranker() as (url, requests):
        HTTPReranker(endpoint_url=url, model="test").rerank("query", ("document",))

    assert requests[0][1] is None


def test_http_reranker_skips_request_for_empty_inputs():
    with _serve_reranker() as (url, requests):
        assert HTTPReranker(endpoint_url=url, model="test").rerank("query", ()) == ()

    assert requests == []


@pytest.mark.parametrize("status", [307, 401, 429, 503])
def test_http_reranker_sanitizes_unsuccessful_http_responses(status):
    with _serve_reranker(b"private-response-content", status=status) as (url, requests):
        provider = HTTPReranker(endpoint_url=url, model="test", api_key="private-token")
        with pytest.raises(RerankerError) as caught:
            provider.rerank("private-query", ("private-content",))

    error = caught.value
    assert isinstance(error, ComponentError)
    assert error.code == "reranker_error"
    assert error.reason == "http_status"
    assert error.status_code == status
    assert "private-" not in str(error)
    assert len(requests) == 1


def test_http_reranker_classifies_timeout():
    with _serve_reranker(hold_response=True) as (url, requests):
        provider = HTTPReranker(endpoint_url=url, model="test", timeout=0.2)
        with pytest.raises(RerankerError) as caught:
            provider.rerank("query", ("document",))

    assert caught.value.reason == "timeout"
    assert len(requests) == 1


def test_http_reranker_classifies_a_closed_connection():
    with _serve_reranker(disconnect=True) as (url, _):
        with pytest.raises(RerankerError) as caught:
            HTTPReranker(endpoint_url=url, model="test").rerank("query", ("document",))

    assert caught.value.reason == "transport"


def test_http_reranker_rejects_a_response_over_the_byte_limit(monkeypatch):
    monkeypatch.setattr("agent_components.models.providers.reranking._MAX_RESPONSE_BYTES", 100)
    body = b'{"results":[{"index":0,"relevance_score":0.5}]}' + b" " * 100
    with _serve_reranker(body) as (url, _):
        with pytest.raises(RerankerError) as caught:
            HTTPReranker(endpoint_url=url, model="test").rerank("query", ("document",))

    assert caught.value.reason == "invalid_response"


@pytest.mark.parametrize(
    "body",
    [
        b"private-response-content",
        b"",
        b"[]",
        b"{}",
        b'{"results": null}',
        b'{"results": []}',
        b'{"results": [{"index": 0, "relevance_score": 0.5}]}',
        b'{"results": [{"index": 0, "relevance_score": 0.5},{"index": 0, "relevance_score": 0.8}]}',
        b'{"results": [{"index": 3, "relevance_score": 0.5},{"index": 1, "relevance_score": 0.8}]}',
        b'{"results": [{"index": -1, "relevance_score": 0.5},'
        b'{"index": 1, "relevance_score": 0.8}]}',
        b'{"results": [{"index": false, "relevance_score": 0.5},'
        b'{"index": 1, "relevance_score": 0.8}]}',
        b'{"results": [{"index": 0, "relevance_score": true},'
        b'{"index": 1, "relevance_score": 0.8}]}',
        b'{"results": [{"index": 0, "relevance_score": "0.5"},'
        b'{"index": 1, "relevance_score": 0.8}]}',
        b'{"results": [{"index": 0, "relevance_score": 1e400},'
        b'{"index": 1, "relevance_score": 0.8}]}',
        b'{"results": [{"index": 0, "relevance_score": NaN},{"index": 1, "relevance_score": 0.8}]}',
        b'{"results": [{"index": 0, "score": 0.5}, {"index": 1, "relevance_score": 0.8}]}',
        b'{"results": [0, {"index": 1, "relevance_score": 0.8}]}',
    ],
)
def test_http_reranker_rejects_malformed_or_incomplete_scores(body):
    with _serve_reranker(body) as (url, _):
        with pytest.raises(RerankerError) as caught:
            HTTPReranker(endpoint_url=url, model="test").rerank("query", ("first", "second"))

    assert caught.value.reason == "invalid_response"
    assert "private-response-content" not in str(caught.value)


def test_http_reranker_reports_missing_configured_key_without_making_request(monkeypatch):
    monkeypatch.delenv("TEST_RERANKER_TOKEN", raising=False)
    with _serve_reranker() as (url, requests):
        provider = HTTPReranker(endpoint_url=url, model="test", api_key_env="TEST_RERANKER_TOKEN")
        with pytest.raises(RerankerError) as caught:
            provider.rerank("query", ("document",))

    assert caught.value.reason == "configuration"
    assert requests == []


@pytest.mark.parametrize(
    "options",
    [
        {"endpoint_url": "/v2/rerank"},
        {"endpoint_url": "ftp://localhost/v2/rerank"},
        {"endpoint_url": "http://private-credential@localhost/v2/rerank"},
        {"endpoint_url": "http://:private-credential@localhost/v2/rerank"},
        {"endpoint_url": "http://localhost:bad/v2/rerank"},
        {"endpoint_url": "http://localhost/v2/rerank#fragment"},
        {"model": " "},
        {"timeout": 0.0},
        {"timeout": float("nan")},
        {"api_key": "private-token", "api_key_env": "TEST_RERANKER_TOKEN"},
    ],
)
def test_http_reranker_rejects_invalid_configuration(options):
    configuration = {"endpoint_url": "http://localhost/v2/rerank", "model": "test", **options}
    with pytest.raises(RerankerError) as caught:
        HTTPReranker(**configuration)

    assert caught.value.reason == "configuration"
    assert "private-" not in str(caught.value)
