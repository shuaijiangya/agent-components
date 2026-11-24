import pytest
from pydantic import ValidationError

from agent_components.core.contracts import Message, ModelOutput, ModelRequest
from agent_components.core.errors import RoutingError, UnsupportedCapabilityError
from agent_components.models import ChatRequest, ModelClient, routing


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


def structured(**kwargs):
    return ModelRequest(
        messages=(Message(role="user", text="text"),), schema={"type": "object"}, **kwargs
    )


async def test_generate_routes_using_assembled_system_prompt_or_callers_full_token_count():
    client = ModelClient(
        {name: Backend(name) for name in ("small", "large")},
        default_alias="small",
        profiles={
            "small": routing.ModelProfile(context_window_tokens=80),
            "large": routing.ModelProfile(context_window_tokens=2000),
        },
        system_prompt="完整系统指令" * 20,
    )
    assert (await client.generate(structured(max_tokens=6))).data == {"backend": "large"}
    assert (await client.generate(structured(max_tokens=6, input_tokens=74))).data == {
        "backend": "small"
    }
    assert (await client.generate(structured(max_tokens=6, input_tokens=75))).data == {
        "backend": "large"
    }


def test_only_eligible_fallback_aliases_are_returned_in_configured_order():
    aliases = ("small", "no_method", "no_tags", "tight", "large", "unused")
    models = {alias: Backend(alias) for alias in aliases}
    models["no_method"] = object()
    profiles = {
        alias: routing.ModelProfile(capabilities=("local",), context_window_tokens=100)
        for alias in aliases
    }
    profiles["small"] = routing.ModelProfile(capabilities=("local",), context_window_tokens=9)
    profiles["tight"] = routing.ModelProfile(capabilities=("local",), context_window_tokens=10)
    profiles["no_tags"] = routing.ModelProfile()
    router = routing._ModelRouter(
        models,
        "unused",
        {"ocr": "small"},
        profiles,
        {"small": ["no_method", "no_tags", "tight", "large"]},
    )
    request = structured(task="ocr", max_tokens=4, required_capabilities=("local",))
    candidates = router.candidates(request, operation="generate", input_tokens=6)
    assert candidates == [("tight", models["tight"]), ("large", models["large"])]


def test_implicit_selection_can_find_a_compatible_profile_without_enabling_runtime_failover():
    aliases = ("default", "no_window", "first", "second")
    models = {alias: Backend(alias) for alias in aliases}
    profiles = {
        alias: routing.ModelProfile(capabilities=("local",), context_window_tokens=10)
        for alias in ("first", "second")
    }
    profiles["no_window"] = routing.ModelProfile(capabilities=("local",))
    router = routing._ModelRouter(models, "default", None, profiles=profiles)
    candidates = router.candidates(
        structured(required_capabilities=("local",)), operation="generate", input_tokens=5
    )
    assert candidates == [("first", models["first"])]


def test_discovered_model_uses_its_own_ordered_fallback_route():
    models = {alias: Backend(alias) for alias in ("tiny", "medium", "large", "backup")}
    profiles = {alias: routing.ModelProfile(context_window_tokens=10) for alias in models}
    profiles["tiny"] = routing.ModelProfile(context_window_tokens=1)
    router = routing._ModelRouter(
        models, "tiny", None, profiles=profiles, fallback_routes={"medium": ["backup", "large"]}
    )
    assert router.candidates(structured(), operation="generate", input_tokens=7) == [
        ("medium", models["medium"]),
        ("backup", models["backup"]),
        ("large", models["large"]),
    ]


def test_preferred_route_carries_the_source_of_selection():
    models = {alias: Backend(alias) for alias in ("small", "large")}
    router = routing._ModelRouter(models, "small", {"ocr": "large"})
    assert router.preferred(structured()) == ("small", "default")
    assert router.preferred(structured(task="ocr")) == ("large", "task_rule")
    assert router.preferred(structured(task="ocr", model_alias="small")) == (
        "small",
        "explicit_alias",
    )


def test_fallback_aliases_require_a_sequence_of_aliases():
    with pytest.raises(RoutingError):
        routing._ModelRouter(
            {"a": Backend("a"), "b": Backend("b")}, "a", None, fallback_routes={"a": "b"}
        )


def test_explicit_alias_cannot_be_replaced_during_capability_or_size_selection():
    models = {alias: Backend(alias) for alias in ("small", "large")}
    router = routing._ModelRouter(
        models,
        "large",
        None,
        profiles={
            "small": routing.ModelProfile(context_window_tokens=5),
            "large": routing.ModelProfile(capabilities=("local",)),
        },
        fallback_routes={"small": ["large"]},
    )
    with pytest.raises(UnsupportedCapabilityError, match="local"):
        router.candidates(
            structured(model_alias="small", required_capabilities=("local",)),
            operation="generate",
            input_tokens=1,
        )
    with pytest.raises(RoutingError, match="context"):
        router.candidates(structured(model_alias="small"), operation="generate", input_tokens=6)
    candidates = router.candidates(
        structured(model_alias="small"), operation="generate", input_tokens=5
    )
    assert candidates == [("small", models["small"]), ("large", models["large"])]


def test_structured_stream_checks_stream_chat_method_on_backend():
    class StreamingOnly:
        async def stream_chat(self, request, *, json_mode=False):
            yield

    models = {"structured": Backend("structured"), "streaming": StreamingOnly()}
    router = routing._ModelRouter(
        models, "structured", None, fallback_routes={"structured": ["streaming"]}
    )
    assert router.candidates(structured(), operation="stream_generate", input_tokens=1) == [
        ("streaming", models["streaming"])
    ]
    with pytest.raises(UnsupportedCapabilityError, match="streaming"):
        router.candidates(
            structured(model_alias="structured"), operation="stream_generate", input_tokens=1
        )


@pytest.mark.parametrize(
    "config",
    [
        {"profiles": {"missing": {}}},
        {"fallback_routes": {"missing": ["one"]}},
        {"fallback_routes": {"one": ["missing"]}},
    ],
)
def test_unknown_alias_in_profile_or_fallback_is_rejected_at_assembly(config):
    with pytest.raises(RoutingError, match="missing"):
        routing._ModelRouter({"one": Backend("one")}, "one", None, **config)


@pytest.mark.parametrize("contract", [ChatRequest, ModelRequest])
def test_input_token_count_accepts_zero_and_requires_nonnegative_integer(contract):
    kwargs = {"messages": (Message(role="user", text="x"),)}
    if contract is ModelRequest:
        kwargs["schema"] = {"type": "object"}
    assert contract(**kwargs, input_tokens=0, required_capabilities=("local",)).input_tokens == 0
    for invalid in (-1, True, 1.5):
        with pytest.raises(ValidationError):
            contract(**kwargs, input_tokens=invalid)
