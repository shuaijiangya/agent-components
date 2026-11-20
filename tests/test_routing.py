import pytest

from agent_components.core.contracts import Message, ModelOutput, ModelRequest
from agent_components.core.errors import RoutingError
from agent_components.models import ModelClient


class Backend:
    def __init__(self, name):
        self.name = name

    async def generate(self, request):
        return ModelOutput(data={"backend": self.name})


async def test_explicit_alias_overrides_task_route_and_default():
    client = ModelClient(
        {"small": Backend("small"), "large": Backend("large")},
        default_alias="small",
        task_routes={"extraction": "large"},
    )
    base = ModelRequest(
        messages=(Message(role="user", text="text"),), schema={"type": "object"}, task="extraction"
    )
    assert (await client.generate(base)).data == {"backend": "large"}
    assert (await client.generate(base.model_copy(update={"model_alias": "small"}))).data == {
        "backend": "small"
    }
    with pytest.raises(RoutingError):
        await client.generate(base.model_copy(update={"model_alias": "unknown"}))


def test_misconfigured_route_fails_during_assembly():
    with pytest.raises(RoutingError):
        ModelClient({}, default_alias="unregistered")


async def test_delegating_structured_backend_still_supports_generate():
    class Proxy:
        def __init__(self):
            self.backend = Backend("proxy")

        def __getattr__(self, name):
            return getattr(self.backend, name)

    client = ModelClient({"proxy": Proxy()}, default_alias="proxy")
    request = ModelRequest(messages=(Message(role="user", text="text"),), schema={"type": "object"})
    assert (await client.generate(request)).data == {"backend": "proxy"}
