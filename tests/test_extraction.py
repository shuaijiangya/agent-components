import json
import logging

import pytest
from pydantic import BaseModel, field_validator

from agent_components.core.contracts import Document, ExtractionOptions, ModelOutput
from agent_components.core.errors import InvalidSchemaError, UnsupportedCapabilityError
from agent_components.extraction import Extractor
from agent_components.models import ModelClient

SCHEMA = {
    "type": "object",
    "properties": {"amount": {"type": "integer", "minimum": 0}},
    "required": ["amount"],
    "additionalProperties": False,
}


class Backend:
    def __init__(self, data):
        self.data = data
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return ModelOutput(data=self.data, model="unit-backend")


def component(data):
    backend = Backend(data)
    return Extractor(ModelClient({"main": backend}, default_alias="main")), backend


async def test_schema_result_is_returned_without_an_agent_or_database():
    extractor, backend = component({"amount": 3200})
    result = await extractor.extract(Document(id="contract-a", text="合同金额3200元"), SCHEMA)
    assert result.status == "success"
    assert result.data == {"amount": 3200}
    assert result.input_ids == ("contract-a",)
    assert json.loads(backend.requests[0].messages[-1].text)[0]["input_id"] == "contract-a"


async def test_missing_required_field_is_not_fabricated():
    extractor, _ = component({})
    result = await extractor.extract(Document(id="missing", text="未填写金额"), SCHEMA)
    assert result.status == "failed"
    assert result.data is None
    assert result.candidates == {}
    assert result.errors[0].code == "schema_validation"


async def test_nested_schema_has_stable_json_pointer_error():
    extractor, _ = component({"items": [{"amount": "not an integer"}]})
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": SCHEMA}},
        "required": ["items"],
    }
    result = await extractor.extract(Document(id="x", text="条目"), schema)
    assert result.errors[0].path == "/items/0/amount"


async def test_invalid_schema_rejected_before_model_call():
    extractor, backend = component({})
    with pytest.raises(InvalidSchemaError):
        await extractor.extract(Document(id="x", text="文本"), {"type": "not-a-json-type"})
    assert not backend.requests


async def test_evidence_requirement_never_silently_falls_back():
    extractor, backend = component({})
    with pytest.raises(UnsupportedCapabilityError):
        await extractor.extract(
            Document(id="x", text="文本"), SCHEMA, ExtractionOptions(strategy="evidence")
        )
    assert not backend.requests


async def test_pydantic_custom_business_validator_is_preserved():
    class Output(BaseModel):
        amount: int

        @field_validator("amount")
        @classmethod
        def enforce_increment(cls, value):
            if value % 100:
                raise ValueError("amount must be a multiple of 100")
            return value

    extractor, _ = component({"amount": 125})
    result = await extractor.extract(Document(id="x", text="金额125元"), Output)
    assert result.status == "failed"
    assert result.errors[0].code == "pydantic_validation"


async def test_logs_do_not_include_document_or_result_body(caplog):
    extractor, _ = component({"amount": 3200})
    with caplog.at_level(logging.INFO, logger="agent_components.model_calls"):
        await extractor.extract(Document(id="x", text="private-document-body"), SCHEMA)
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "agent_components.model_calls"
    ]
    assert [e["event"] for e in events] == ["model.started", "model.completed"]
    assert "private-document-body" not in caplog.text
    assert "3200" not in caplog.text
