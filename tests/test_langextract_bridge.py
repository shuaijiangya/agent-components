import asyncio
import json

import pytest

pytest.importorskip("langextract")

from agent_components.adapters.langextract import create_provider
from agent_components.core.contracts import ModelOutput
from agent_components.models import ModelClient

pytestmark = pytest.mark.integration


class Backend:
    async def generate(self, request):
        return ModelOutput(data={"extractions": []})


async def test_native_provider_bridge_uses_owning_loop_without_blocking():
    provider = create_provider(
        ModelClient({"model": Backend()}, default_alias="model"),
        loop=asyncio.get_running_loop(),
        output_schema={"type": "object"},
    )
    with pytest.raises(RuntimeError, match="to_thread"):
        list(provider.infer(["prompt"]))
    result = await asyncio.to_thread(lambda: list(provider.infer(["prompt-1", "prompt-2"])))
    assert len(result) == 2
    assert json.loads(result[0][0].output) == {"extractions": []}
