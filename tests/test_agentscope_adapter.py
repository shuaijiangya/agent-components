import json

import pytest

pytest.importorskip("agentscope")

from agentscope.agent import Agent
from agentscope.credential import OpenAICredential
from agentscope.message import ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionEngine,
    PermissionRule,
)
from agentscope.tool import FunctionTool
from pydantic import SecretStr

from agent_components.adapters.agentscope.model import AgentScopeStructuredModel
from agent_components.adapters.agentscope.runtime import (
    AgentScopeContextAdapter,
    AgentScopePermissionAdapter,
)
from agent_components.adapters.agentscope.skills import AgentScopeSkillAdapter
from agent_components.core.contracts import Message, ModelRequest, RuntimeRef

pytestmark = pytest.mark.integration


class NativeModel(ChatModelBase):
    def __init__(self):
        super().__init__(
            credential=OpenAICredential(api_key=SecretStr("unused-test-value")),
            model="native-contract-test",
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
        )
        self.calls = 0

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        assert tools[0]["function"]["name"] == "generate_structured_output"
        return ChatResponse(
            content=[
                ToolCallBlock(
                    id="call-1",
                    name="generate_structured_output",
                    input=json.dumps({"amount": 3200}),
                )
            ],
            is_last=True,
            usage=ChatUsage(input_tokens=10, output_tokens=5, time=0.1),
        )


async def test_actual_native_structured_output_contract_without_paid_request():
    native = NativeModel()
    adapter = AgentScopeStructuredModel(native)
    result = await adapter.generate(
        ModelRequest(
            messages=(Message(role="user", text="合同金额3200元"),),
            schema={
                "type": "object",
                "properties": {"amount": {"type": "integer"}},
                "required": ["amount"],
            },
        )
    )
    assert native.calls == 1
    assert result.data == {"amount": 3200}
    assert result.usage.input_tokens == 10


async def test_native_context_state_is_wrapped_with_backend_version():
    model = NativeModel()
    agent = Agent(name="context-contract", system_prompt="Use supplied context.", model=model)
    runtime = RuntimeRef(id="runtime-a", backend="agentscope")
    adapter = AgentScopeContextAdapter(runtime, agent)
    snapshot = await adapter.compact(runtime)
    assert snapshot.backend == "agentscope"
    assert snapshot.schema_version == "2.0.11"
    assert "session_id" in snapshot.payload
    assert model.calls == 0
    with pytest.raises(ValueError):
        await adapter.snapshot(RuntimeRef(id="other", backend="agentscope"))


async def test_native_permission_deny_takes_priority_over_readonly():
    def lookup(record_id: str):
        return {"id": record_id}

    tool = FunctionTool(lookup, name="lookup", is_read_only=True)
    engine = PermissionEngine(PermissionContext())
    adapter = AgentScopePermissionAdapter(engine)
    assert (await adapter.check(tool, {"record_id": "x"})).status == "allow"
    engine.add_rule(
        PermissionRule(
            tool_name="lookup",
            rule_content=None,
            behavior=PermissionBehavior.DENY,
            source="projectSettings",
        )
    )
    assert (await adapter.check(tool, {"record_id": "x"})).status == "deny"


async def test_skill_resource_changes_invalidate_content_version(tmp_path):
    skill = tmp_path / "extract-contract"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: extract-contract\ndescription: Extract contract fields\n---\nRead references.\n"
    )
    resource = skill / "reference.txt"
    resource.write_text("version one")
    adapter = AgentScopeSkillAdapter()
    before = await adapter.list_skills(str(tmp_path))
    resource.write_text("version two")
    after = await adapter.list_skills(str(tmp_path))
    assert before[0].id == after[0].id == "extract-contract"
    assert before[0].version != after[0].version
