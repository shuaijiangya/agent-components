import asyncio
import json
import logging

import pytest

from agent_components.core.contracts import Chunk, Document, ModelOutput
from agent_components.models import MODEL_CALL_LOGGER_NAME, ModelClient
from agent_components.support import ExtractionOptions, Extractor

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
}


class Backend:
    async def generate(self, request):
        if "extractions" in request.schema_["properties"]:
            return ModelOutput(data={"extractions": [{"quote": "Alpha"}]})
        if "citations" in request.schema_["properties"]:
            return ModelOutput(
                data={
                    "data_json": '{"value":"Alpha"}',
                    "citations": [{"path": "/value", "evidence_ids": ["1"]}],
                }
            )
        return ModelOutput(data={"value": "Alpha"})


def chunk(key, document, revision):
    return Chunk(
        id=key, document_id=document, revision_id=revision, text="Alpha", start=0, end=5, ordinal=0
    )


def events(caplog, event_name):
    records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == MODEL_CALL_LOGGER_NAME
    ]
    return [record for record in records if record["event"] == event_name]


@pytest.mark.parametrize(
    ("content", "document_id", "revision_id"),
    [
        (Document(id="contract", revision_id="v7", text="Alpha"), "contract", "v7"),
        ([chunk("a", "one", "v2"), chunk("b", "one", "v2")], "one", "v2"),
        ([chunk("a", "one", "v2"), chunk("b", "one", "v3")], "one", None),
        ([chunk("a", "one", "v2"), chunk("b", "two", "v2")], None, None),
    ],
)
async def test_structured_calls_identify_only_shared_sources(
    content, document_id, revision_id, caplog
):
    extractor = Extractor(ModelClient({"model": Backend()}, default_alias="model"))
    with caplog.at_level(logging.INFO, logger=MODEL_CALL_LOGGER_NAME):
        result = await extractor.extract(content, SCHEMA)

    assert result.status == "success"
    assert result.meta.request_id
    for name in ("model.started", "model.attempt.started", "model.completed"):
        (event,) = events(caplog, name)
        assert event["request_id"] == result.meta.request_id
        assert event.get("document_id") == document_id
        assert event.get("revision_id") == revision_id
        assert event.get("document_version") == revision_id


@pytest.mark.integration
@pytest.mark.parametrize("request_id", [None, "extract-request"])
async def test_native_evidence_model_calls_inherit_workflow_and_bind_segment_revisions(
    request_id, caplog
):
    pytest.importorskip("langextract")
    from agent_components.models.call_logging import model_log_context

    extractor = Extractor(ModelClient({"model": Backend()}, default_alias="model"))
    with (
        caplog.at_level(logging.INFO, logger=MODEL_CALL_LOGGER_NAME),
        model_log_context(
            request_id="outer-request",
            run_id="run",
            job_id="job",
            kb_id="kb",
            document_id="outer-doc",
            revision_id="outer-revision",
            document_version="outer-version",
        ),
    ):
        result = await extractor.extract(
            [chunk("a", "one", "v2"), chunk("b", "two", "v5")],
            SCHEMA,
            ExtractionOptions(strategy="evidence", request_id=request_id),
        )

    assert result.status == "success"
    assert result.meta.request_id == (request_id or "outer-request")
    for name in ("model.started", "model.attempt.started", "model.completed"):
        calls = events(caplog, name)
        assert [
            (e.get("document_id"), e.get("revision_id"), e.get("document_version")) for e in calls
        ] == [("one", "v2", "v2"), ("two", "v5", "v5"), (None, None, None)]
        assert {e["request_id"] for e in calls} == {result.meta.request_id}
        assert {(e["run_id"], e["job_id"], e["kb_id"]) for e in calls} == {("run", "job", "kb")}


@pytest.mark.integration
async def test_cancelled_langextract_attempt_retains_segment_identity(caplog):
    pytest.importorskip("langextract")
    started, stopped = asyncio.Event(), asyncio.Event()

    class BlockingBackend:
        async def generate(self, request):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    extractor = Extractor(ModelClient({"model": BlockingBackend()}, default_alias="model"))
    with caplog.at_level(logging.INFO, logger=MODEL_CALL_LOGGER_NAME):
        task = asyncio.create_task(
            extractor.extract(
                [chunk("a", "doc", "v3"), chunk("b", "other", "v9")],
                SCHEMA,
                ExtractionOptions(strategy="evidence", request_id="cancel-me"),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stopped.wait(), timeout=5)

    (start,) = events(caplog, "model.attempt.started")
    (cancelled,) = events(caplog, "model.attempt.cancelled")
    assert cancelled["attempt_id"] == start["attempt_id"]
    assert cancelled["request_id"] == "cancel-me"
    assert (cancelled["document_id"], cancelled["revision_id"]) == ("doc", "v3")
