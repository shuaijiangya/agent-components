import asyncio
import json
import logging
from contextlib import aclosing

import pytest
from pydantic import SecretStr

from agent_components.core.contracts import Usage
from agent_components.models import call_logging as logs


def events(caplog):
    return [
        json.loads(r.getMessage()) for r in caplog.records if r.name == logs.MODEL_CALL_LOGGER_NAME
    ]


def test_retry_fallback_and_failure_keep_identifiers_usage_and_safe_metadata(caplog):
    caplog.set_level(logging.INFO, logger=logs.MODEL_CALL_LOGGER_NAME)
    with pytest.raises(ValueError, match="private-answer"):
        with logs.ModelCall("test", operation="chat", alias="first", body="private-input") as call:
            call.event("model.route", model_alias="first", reason="explicit_alias")
            with pytest.raises(TimeoutError), call.attempt("first"):
                raise TimeoutError("private-timeout")
            call.event("model.retry", error_type="TimeoutError", raw_exception="private-error")
            call.event("model.fallback", from_alias="first", to_alias="second")
            with call.attempt("second", model="actual") as attempt:
                attempt.complete(Usage(input_tokens=6))
            raise ValueError("private-answer")
    rows = events(caplog)
    assert [row["event"] for row in rows] == [
        "model.started",
        "model.route",
        "model.attempt.started",
        "model.attempt.failed",
        "model.retry",
        "model.fallback",
        "model.attempt.started",
        "model.attempt.completed",
        "model.failed",
    ]
    assert all(row["request_id"] and row["call_id"] and row["timestamp"] for row in rows)
    assert len({row["call_id"] for row in rows}) == 1
    assert len({row["request_id"] for row in rows}) == 1
    assert rows[2]["attempt_id"] == rows[3]["attempt_id"] != rows[6]["attempt_id"]
    assert rows[2]["attempt_number"] == 1 and rows[6]["attempt_number"] == 2
    assert rows[-1]["model_alias"] == "second"
    assert rows[-1]["usage"]["input_tokens"] == 6
    assert rows[-1]["retry_count"] == rows[-1]["fallback_count"] == 1
    assert rows[-1]["error_type"] == "ValueError"
    assert rows[3]["error_code"] == "timeout"
    assert rows[-1]["duration_seconds"] >= 0
    assert rows[0]["input_length"] == len("private-input")
    assert "private-" not in caplog.text


async def test_nested_context_can_clear_versions_and_isolate_concurrent_calls(caplog):
    caplog.set_level(logging.INFO, logger=logs.MODEL_CALL_LOGGER_NAME)

    async def log(run):
        with logs.model_log_context(run_id=run, request_id=f"req-{run}", document_version=None):
            await asyncio.sleep(0)
            assert logs.current_request_id() == f"req-{run}"
            with logs.ModelCall("test", operation="embedding") as call:
                call.complete()

    with logs.model_log_context(document_version="parent", job_id="job", request_id="parent-id"):
        await asyncio.gather(log("a"), log("b"))
        with logs.ModelCall("test", operation="rerank", request_id="override") as call:
            call.complete()

    rows = [row for row in events(caplog) if row["event"] == "model.started"]
    assert [(row["run_id"], row["request_id"]) for row in rows[:2]] == [
        ("a", "req-a"),
        ("b", "req-b"),
    ]
    assert all(row["job_id"] == "job" for row in rows)
    assert rows[0].get("document_version") is None and rows[1].get("document_version") is None
    assert rows[-1]["document_version"] == "parent" and rows[-1]["request_id"] == "override"
    assert logs.current_request_id() is None


async def test_generator_closed_in_another_task_emits_one_cancelled_attempt_and_call(caplog):
    caplog.set_level(logging.INFO, logger=logs.MODEL_CALL_LOGGER_NAME)

    async def stream():
        with logs.ModelCall("test", operation="stream_chat") as call, call.attempt("s") as attempt:
            yield "delta"
            attempt.complete()
            call.complete()

    with logs.model_log_context(request_id="initial"):
        async with aclosing(stream()) as output:
            assert await asyncio.create_task(anext(output)) == "delta"
        assert logs.current_request_id() == "initial"
    rows = events(caplog)
    assert [row["event"] for row in rows] == [
        "model.started",
        "model.attempt.started",
        "model.attempt.cancelled",
        "model.cancelled",
    ]
    assert {row["request_id"] for row in rows} == {"initial"}


@pytest.fixture
def configure(monkeypatch):
    monkeypatch.setattr(logs, "_sink", None)
    monkeypatch.setattr(logs, "_include_bodies", False)
    yield logs.configure_model_logging
    logs.configure_model_logging(destination="stdout")


def test_opt_in_stdout_scrubs_nested_fields_echoed_credentials_and_free_text(configure, capsys):
    configure(include_bodies=True)
    with logs.ModelCall(
        "test",
        operation="chat",
        secrets=[SecretStr("opaque!!!42")],
        body={
            "text": "retain me",
            "messages": [{"Authorization": "Bearer hidden-auth", "apiKey": "other-secret"}],
        },
    ) as call:
        call.complete(
            body={
                "opaque!!!42": "includes opaque!!!42 and Bearer unseen-token and api_key=free-key",
                "text": 'credentials {"access_token":"embedded-secret"}',
                "list": ["sk-test-abcdefgh123456789", "other-secret"],
            }
        )
    output = capsys.readouterr().out
    for secret in [
        "opaque!!!42",
        "hidden-auth",
        "other-secret",
        "unseen-token",
        "free-key",
        "embedded-secret",
        "sk-test-abcdefgh123456789",
    ]:
        assert secret not in output
    request, result = map(json.loads, output.splitlines())
    assert request["request_body"]["text"] == "retain me"
    assert "response_body" in result


def test_configured_jsonl_rotates_and_bounds_retention_without_stdout(configure, tmp_path, capsys):
    path = tmp_path / "calls.jsonl"
    configure(destination="jsonl", path=path, max_bytes=1200, backup_count=2)
    for i in range(30):
        with logs.ModelCall("test", operation="chat", request_id=f"req-{i}") as call:
            call.complete(body="private-result")
    assert capsys.readouterr().out == ""
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "calls.jsonl",
        "calls.jsonl.1",
        "calls.jsonl.2",
    ]
    rows = [json.loads(line) for p in tmp_path.iterdir() for line in p.read_text().splitlines()]
    assert any(row["request_id"] == "req-29" and row["event"] == "model.completed" for row in rows)
    assert all("response_body" not in row and row["request_id"] != "req-0" for row in rows)


def test_write_failure_does_not_change_model_outcome_or_disclose_payload(
    configure, tmp_path, capsys
):
    path = tmp_path / "calls.jsonl"
    configure(destination="jsonl", path=path, max_bytes=1, backup_count=1, include_bodies=True)
    (tmp_path / "calls.jsonl.1").mkdir()  # 所有轮转重命名都会失败。
    with logs.ModelCall("test", operation="chat", body="private-body") as call:
        call.complete()
    output = capsys.readouterr()
    assert "model call log write failed" in output.err
    assert "private-body" not in output.err


async def test_observed_http_sends_are_bound_to_attempt_and_second_send_is_blocked(caplog):
    caplog.set_level(logging.INFO, logger=logs.MODEL_CALL_LOGGER_NAME)
    with logs.ModelCall("t", operation="chat") as call:
        with call.attempt("one") as attempt:
            logs.record_model_http_request()
            with attempt.bind():
                await asyncio.to_thread(logs.record_model_http_request)
                with pytest.raises(RuntimeError, match="one HTTP request"):
                    logs.record_model_http_request()
            attempt.complete()
        logs.record_model_http_request()
        call.complete()
    rows = events(caplog)
    (wire,) = [row for row in rows if row["event"] == "model.http.request"]
    (finished,) = [row for row in rows if row["event"] == "model.attempt.completed"]
    assert wire["attempt_id"] == finished["attempt_id"]
    assert finished["http_requests"] == 1
