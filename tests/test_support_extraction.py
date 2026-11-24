import asyncio
import json

import pytest
from pydantic import RootModel

from agent_components.core.contracts import (
    Chunk,
    Document,
    ModelOutput,
    SourceRef,
    SourceSpan,
    Usage,
)
from agent_components.core.errors import InvalidSchemaError
from agent_components.models import ModelClient

SCHEMA = {
    "type": "object",
    "properties": {"contracts": {"type": "array", "items": {"type": "object"}}},
    "required": ["contracts"],
}


class Backend:
    def __init__(self, generate):
        self.generate_response = generate
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        data = await self.generate_response(request)
        return ModelOutput(data=data, usage=Usage(input_tokens=5, output_tokens=3), model="fixture")


def extractor(generate):
    from agent_components.support import Extractor

    backend = Backend(generate)
    return Extractor(ModelClient({"default": backend}, default_alias="default")), backend


async def test_support_extracts_non_object_root_and_reports_execution_metadata():
    async def generate(request):
        assert request.max_tokens == 64
        return {"data_json": "[3200]"}

    from agent_components.support import ExtractionOptions

    component, _ = extractor(generate)
    result = await component.extract(
        Document(id="contract", revision_id="r2", text="合同金额3200元"),
        RootModel[list[int]],
        ExtractionOptions(max_output_tokens=64, request_id="business-request"),
    )
    assert result.status == "success"
    assert result.data == [3200]
    assert result.meta.request_id == "business-request"
    assert result.meta.models == ("fixture",)
    assert result.meta.coverage[0].revision_id == "r2"
    assert result.usage == Usage(input_tokens=5, output_tokens=3)


async def test_support_normalizes_provider_timeout():
    async def generate(request):
        await asyncio.sleep(60)

    from agent_components.support import ExtractionOptions

    component, _ = extractor(generate)
    result = await component.extract(
        Document(id="one", text="合同"), SCHEMA, ExtractionOptions(timeout_seconds=0.05)
    )
    assert result.status == "failed"
    assert result.errors[0].code == "extraction_timeout"
    assert result.meta.coverage[0].status == "failed"


async def test_schema_reference_validation_stays_within_supplied_resources(tmp_path):
    external = tmp_path / "schema.json"
    external.write_text('{"type":"integer"}')
    schema = {
        "type": "object",
        "properties": {"amount": {"$ref": external.as_uri()}},
        "required": ["amount"],
    }

    async def generate(request):
        return {"data_json": '{"amount":3200}'}

    component, _ = extractor(generate)
    with pytest.raises(InvalidSchemaError, match="could not be resolved locally"):
        await component.extract(Document(id="ref", text="合同金额3200元"), schema)


@pytest.mark.integration
async def test_native_langextract_returns_nested_field_evidence_and_original_offsets():
    pytest.importorskip("langextract")
    from agent_components.support import ExtractionOptions

    text = "甲方合同，合同金额为3200元。"
    start = text.index("3200")
    source = SourceRef(input_id="pdf", page=3, precision="element", bbox=(0, 1, 2, 3))

    async def generate(request):
        if "extractions" in request.schema_["properties"]:
            return {"extractions": [{"quote": "3200元"}]}
        payload = json.loads(request.messages[-1].text)
        evidence_id = payload["evidence"][0]["id"]
        return {
            "data_json": '{"contracts":[{"amount/y~":3200}]}',
            "citations": [{"path": "/contracts/0/amount~1y~0", "evidence_ids": [evidence_id]}],
        }

    component, backend = extractor(generate)
    result = await component.extract(
        Document(
            id="pdf",
            revision_id="rev-3",
            text=text,
            source_map=(SourceSpan(start=0, end=len(text), refs=(source,)),),
        ),
        SCHEMA,
        ExtractionOptions(strategy="evidence"),
    )
    assert result.status == "success", result.errors
    assert result.data == {"contracts": [{"amount/y~": 3200}]}
    (evidence,) = result.evidence
    assert evidence.field_path == "/contracts/0/amount~1y~0"
    assert evidence.alignment == "exact"
    assert evidence.quote == text[evidence.start : evidence.end] == "3200元"
    assert evidence.start == start
    assert evidence.source_refs[0].revision_id == "rev-3"
    assert evidence.source_refs[1].page == 3
    assert evidence.source_refs[1].precision == "element"
    assert [request.task for request in backend.requests] == ["document_extraction"] * 2
    assert all(request.request_id == result.meta.request_id for request in backend.requests)
    assert result.usage == Usage(input_tokens=10, output_tokens=6)


@pytest.mark.integration
async def test_long_input_keeps_successful_segments_and_reports_failed_segment():
    pytest.importorskip("langextract")
    from agent_components.support import EvidenceOptions, ExtractionOptions

    async def generate(request):
        if "extractions" in request.schema_["properties"]:
            prompt = request.messages[-1].text
            if "BAD-SEGMENT" in prompt:
                raise RuntimeError("provider error containing private text")
            return {"extractions": [{"quote": "合同A"}] if "合同A" in prompt else []}
        payload = json.loads(request.messages[-1].text)
        return {
            "data_json": '{"contracts":[{"name":"合同A"}]}',
            "citations": [
                {"path": "/contracts/0/name", "evidence_ids": [payload["evidence"][0]["id"]]}
            ],
        }

    text = "合同A已授权。\n\n" + "业务条款。" * 8 + "\n\nBAD-SEGMENT.\n\n" + "合同其他条款。" * 8
    component, backend = extractor(generate)
    result = await component.extract(
        Chunk(
            id="chunk",
            document_id="doc",
            revision_id="r3",
            text=text,
            start=100,
            end=100 + len(text),
            ordinal=1,
        ),
        SCHEMA,
        ExtractionOptions(strategy="evidence", evidence=EvidenceOptions(max_char_buffer=32)),
    )
    assert result.status == "partial"
    assert result.data == {"contracts": [{"name": "合同A"}]}
    assert result.errors[0].code == "segment_model_error"
    assert result.errors[0].segment is not None
    assert result.errors[0].input_id == "chunk"
    assert "private text" not in result.model_dump_json()
    assert len(backend.requests) > 3
    assert result.meta.coverage[0].status == "partial"
    assert result.evidence[0].source_refs[0].start == 100
    assert result.usage.input_tokens is None


@pytest.mark.integration
async def test_evidence_budget_and_invalid_citations_never_report_full_success():
    pytest.importorskip("langextract")
    from agent_components.support import EvidenceOptions, ExtractionOptions

    async def generate(request):
        if "extractions" in request.schema_["properties"]:
            return {"extractions": [{"quote": "合同A"}]}
        return {
            "data_json": '{"contracts":[{"name":"合同A"}]}',
            "citations": [{"path": "/contracts/9/name", "evidence_ids": ["unknown"]}],
        }

    component, backend = extractor(generate)
    result = await component.extract(
        [
            Chunk(
                id=key, document_id="doc", revision_id="r", text="合同A", start=0, end=3, ordinal=i
            )
            for i, key in enumerate(("first", "second"))
        ],
        SCHEMA,
        ExtractionOptions(strategy="evidence", evidence=EvidenceOptions(max_model_calls=2)),
    )
    assert result.status == "partial"
    assert len(backend.requests) == 2
    assert result.evidence == ()
    assert {issue.code for issue in result.errors} == {"extraction_budget_exceeded"}
    assert {issue.code for issue in result.warnings} == {
        "invalid_citation",
        "missing_field_evidence",
    }
    first, second = result.meta.coverage
    assert (first.status, second.status) == ("complete", "failed")


@pytest.mark.integration
async def test_cancelled_evidence_extraction_cancels_inflight_provider():
    pytest.importorskip("langextract")
    from agent_components.support import ExtractionOptions

    started = asyncio.Event()
    stopped = asyncio.Event()

    async def generate(request):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    component, backend = extractor(generate)
    task = asyncio.create_task(
        component.extract(
            Document(id="cancel", text="合同金额300元。"),
            SCHEMA,
            ExtractionOptions(strategy="evidence"),
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(stopped.wait(), 5)
    assert len(backend.requests) == 1


@pytest.mark.integration
async def test_evidence_passes_share_prompt_and_deduplicate_aligned_quotes():
    pytest.importorskip("langextract")
    from agent_components.support import (
        EvidenceOptions,
        ExampleCitation,
        ExtractionExample,
        ExtractionOptions,
    )

    async def generate(request):
        assert request.messages[0].role == "system"
        assert "仅抽取正式编号" in request.messages[0].text
        if "extractions" in request.schema_["properties"]:
            assert "编号Q999" in request.messages[-1].text
            return {"extractions": [{"quote": "编号A123", "quote_attributes": {"hint": "/number"}}]}
        payload = json.loads(request.messages[-1].text)
        assert len(payload["evidence"]) == 1
        return {
            "data_json": '{"number":"A123"}',
            "citations": [{"path": "/number", "evidence_ids": [payload["evidence"][0]["id"]]}],
        }

    component, backend = extractor(generate)
    result = await component.extract(
        Document(id="source", text="正式编号A123。"),
        {"type": "object", "properties": {"number": {"type": "string"}}, "required": ["number"]},
        ExtractionOptions(
            strategy="evidence",
            system_prompt="仅抽取正式编号",
            examples=(
                ExtractionExample(
                    text="编号Q999。",
                    data={"number": "Q999"},
                    evidence=(ExampleCitation(field_path="/number", quote="编号Q999"),),
                ),
            ),
            evidence=EvidenceOptions(extraction_passes=2),
        ),
    )
    assert result.status == "success", result.errors
    assert len(backend.requests) == 3
    assert result.meta.coverage[0].completed_segments == 2
    assert len(result.evidence) == 1


@pytest.mark.integration
async def test_unlocated_quote_is_retained_without_invented_offsets_or_full_success():
    pytest.importorskip("langextract")
    from agent_components.support import EvidenceOptions, ExtractionOptions

    async def generate(request):
        if "extractions" in request.schema_["properties"]:
            return {"extractions": [{"quote": "合同编号ZZZ"}]}
        return {
            "data_json": '{"contracts":[{"number":"ZZZ"}]}',
            "citations": [{"path": "/contracts/0/number", "evidence_ids": ["1"]}],
        }

    component, _ = extractor(generate)
    result = await component.extract(
        Document(id="source", text="合同编号ABC"),
        SCHEMA,
        ExtractionOptions(
            strategy="evidence", evidence=EvidenceOptions(enable_fuzzy_alignment=False)
        ),
    )
    assert result.status == "failed"
    assert result.data is None
    assert result.candidates == {"contracts": [{"number": "ZZZ"}]}
    assert result.errors[0].code == "no_located_evidence"
    (evidence,) = result.evidence
    assert evidence.alignment == "unlocated"
    assert (evidence.start, evidence.end, evidence.source_refs) == (None, None, ())
    assert {item.code for item in result.warnings} == {
        "unlocated_citation",
        "missing_field_evidence",
    }
