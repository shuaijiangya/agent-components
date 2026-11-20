import asyncio
import json
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("agentscope")

from agentscope.credential import OpenAICredential
from agentscope.message import TextBlock, ThinkingBlock, ToolCallBlock
from agentscope.model import (
    ChatModelBase,
    ChatResponse,
    OpenAIChatModel,
    OpenAIResponseModel,
    StructuredResponse,
)
from pydantic import SecretStr

from agent_components.core.contracts import Message, ModelRequest, Usage
from agent_components.core.errors import UnsupportedCapabilityError
from agent_components.models.contracts import ChatRequest
from agent_components.models.providers.agentscope import AgentScopeStructuredModel

pytestmark = pytest.mark.integration


@asynccontextmanager
async def model_endpoint(
    payload, *, keep_open=False, model_type=OpenAIChatModel, path="/v1/chat/completions"
):
    calls = []
    disconnected = asyncio.Event()

    async def handle(reader, writer):
        try:
            headers = (await reader.readuntil(b"\r\n\r\n")).decode().splitlines()
            fields = {
                key.lower(): value
                for key, value in (line.split(":", 1) for line in headers[1:] if line)
            }
            length = int(fields["content-length"])
            request = json.loads(await reader.readexactly(length))
            calls.append(request)
            if headers[0].split()[1] != path:
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return
            if isinstance(payload, list):
                content_type = "text/event-stream"
                body = "".join(
                    f"data: {event if isinstance(event, str) else json.dumps(event)}\n\n"
                    for event in payload
                ).encode()
            else:
                content_type = "application/json"
                body = json.dumps(payload).encode()
            response = (
                f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nConnection: close\r\n\r\n"
            )
            writer.write(response.encode() + body)
            await writer.drain()
            if keep_open:
                await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            disconnected.set()

    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        native = model_type(
            credential=OpenAICredential(
                api_key=SecretStr("localhost-test"), base_url=f"http://127.0.0.1:{port}/v1"
            ),
            model="requested-model",
            stream=False,
            max_retries=0,
            client_kwargs={"max_retries": 0, "timeout": 5},
        )
        async with native.client:
            yield AgentScopeStructuredModel(native), calls, disconnected


def request_with_history():
    return ChatRequest(
        messages=(
            Message(role="system", text="Global instruction"),
            Message(role="system", text="Conversation instruction"),
            Message(role="user", text="Earlier question"),
            Message(role="assistant", text="Earlier answer"),
            Message(role="user", text="Current question"),
        ),
        max_tokens=25,
    )


def assert_history(body):
    messages = body["messages"]
    assert [message["role"] for message in messages] == [
        "system",
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert [message["content"] for message in messages] == [
        "Global instruction",
        "Conversation instruction",
        "Earlier question",
        "Earlier answer",
        "Current question",
    ]
    assert body["max_tokens"] == 25


def completion():
    return {
        "id": "chat-test",
        "object": "chat.completion",
        "created": 1,
        "model": "served-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Visible text",
                    "reasoning_content": "Private reasoning",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    }


def chunk(*choices, usage=None):
    return {
        "id": "chat-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "served-model",
        "choices": choices,
        "usage": usage,
    }


def choice(index=0, finish_reason=None, **delta):
    return {"index": index, "delta": delta, "finish_reason": finish_reason}


async def test_openai_chat_sends_history_and_returns_visible_text_with_usage():
    async with model_endpoint(completion()) as (adapter, calls, _):
        output = await adapter.chat(request_with_history())

    assert output.text == "Visible text"
    assert output.usage == Usage(input_tokens=5, output_tokens=7)
    assert output.model == "served-model"
    assert calls[0]["stream"] is False
    assert_history(calls[0])


@pytest.mark.parametrize("finish_reason", [None, "length", "content_filter", "tool_calls"])
async def test_nonstream_response_requires_successful_finish_reason(finish_reason):
    payload = completion()
    payload["choices"][0]["finish_reason"] = finish_reason
    async with model_endpoint(payload) as (adapter, _, _):
        with pytest.raises(RuntimeError):
            await adapter.chat(request_with_history())


async def test_responses_model_uses_its_native_nonstream_endpoint():
    payload = {
        "id": "responses-test",
        "object": "response",
        "model": "served-model",
        "status": "completed",
        "output": [
            {
                "id": "message-test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Visible text", "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
    }
    request = ChatRequest(messages=(Message(role="user", text="Question"),), max_tokens=25)
    async with model_endpoint(payload, model_type=OpenAIResponseModel, path="/v1/responses") as (
        adapter,
        calls,
        _,
    ):
        output = await adapter.chat(request)

    assert output.text == "Visible text"
    assert output.usage == Usage(input_tokens=5, output_tokens=7)
    assert calls[0]["max_output_tokens"] == 25
    assert calls[0]["input"][0]["role"] == "user"
    assert calls[0]["stream"] is False


async def test_responses_model_reports_streaming_unsupported_before_requesting():
    async with model_endpoint([], model_type=OpenAIResponseModel, path="/v1/responses") as (
        adapter,
        calls,
        _,
    ):
        with pytest.raises(UnsupportedCapabilityError):
            await anext(adapter.stream_chat(request_with_history()))

    assert calls == []


def native_adapter(model_type):
    native = model_type(
        credential=OpenAICredential(api_key=SecretStr("localhost-test")),
        model="native-model",
        parameters=model_type.Parameters(),
        stream=False,
        max_retries=0,
    )
    return AgentScopeStructuredModel(native)


async def test_native_chat_returns_only_text_blocks():
    class NativeModel(ChatModelBase):
        async def _call_api(self, *args, **kwargs):
            assert kwargs["max_tokens"] == 25
            return ChatResponse(
                content=[
                    TextBlock(text="Visible "),
                    ThinkingBlock(thinking="Private reasoning"),
                    ToolCallBlock(id="a", name="hidden_tool", input="{}"),
                    TextBlock(text="text"),
                ],
                is_last=True,
            )

    output = await native_adapter(NativeModel).chat(request_with_history())

    assert output.text == "Visible text"
    assert output.usage == Usage()
    assert output.model == "native-model"


async def test_native_chat_propagates_cancellation():
    entered = asyncio.Event()

    class InterruptibleModel(ChatModelBase):
        async def _call_api(self, *args, **kwargs):
            entered.set()
            await asyncio.Future()

    adapter = native_adapter(InterruptibleModel)
    task = asyncio.create_task(adapter.chat(request_with_history()))
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_sse_yields_choice_zero_text_and_provider_usage():
    events = [
        chunk(choice(role="assistant")),
        chunk(choice(reasoning_content="Private reasoning")),
        chunk(choice(1, "stop", content="Unselected choice"), choice(content='{"a":')),
        chunk(choice(content="1}")),
        chunk(choice(finish_reason="stop")),
        chunk(usage={"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}),
        "[DONE]",
    ]
    async with model_endpoint(events) as (adapter, calls, _):
        deltas = [
            delta async for delta in adapter.stream_chat(request_with_history(), json_mode=True)
        ]

    assert [delta.text for delta in deltas if delta.text] == ['{"a":', "1}"]
    assert [delta.usage for delta in deltas if delta.usage is not None] == [
        Usage(input_tokens=5, output_tokens=7)
    ]
    assert {delta.model for delta in deltas if delta.model is not None} == {"served-model"}
    assert calls[0]["stream"] is True
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert_history(calls[0])


@pytest.mark.parametrize("finish_reason", [None, "length", "content_filter", "tool_calls"])
async def test_sse_rejects_missing_or_unsuccessful_finish_reason(finish_reason):
    events = [chunk(choice(content="Partial text")), chunk(choice(1, "stop"))]
    if finish_reason is not None:
        events.extend([chunk(choice(finish_reason=finish_reason)), "[DONE]"])
    async with model_endpoint(events) as (adapter, _, _):
        stream = adapter.stream_chat(request_with_history())
        assert (await anext(stream)).text == "Partial text"
        with pytest.raises(RuntimeError):
            await anext(stream)


@pytest.mark.parametrize("cancel", [False, True])
async def test_sse_releases_open_connection_when_closed_or_cancelled(cancel):
    async with model_endpoint([chunk(choice(content="First"))], keep_open=True) as (
        adapter,
        _,
        disconnected,
    ):
        stream = adapter.stream_chat(request_with_history())
        assert (await asyncio.wait_for(anext(stream), timeout=3)).text == "First"
        if cancel:
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        else:
            await stream.aclose()
        await asyncio.wait_for(disconnected.wait(), timeout=3)


async def test_structured_only_native_model_reports_unsupported_text_capabilities():
    class StructuredOnly:
        async def generate_structured_output(self):
            raise AssertionError("structured path was called for a text request")

    adapter = AgentScopeStructuredModel(StructuredOnly())
    with pytest.raises(UnsupportedCapabilityError):
        await adapter.chat(request_with_history())
    with pytest.raises(UnsupportedCapabilityError):
        await anext(adapter.stream_chat(request_with_history()))


@pytest.mark.parametrize("limit", [None, 25])
async def test_structured_output_passes_token_limit_only_when_supplied(limit):
    class StructuredModel:
        async def generate_structured_output(self, **kwargs):
            assert ("max_tokens" in kwargs) is (limit is not None)
            assert kwargs.get("max_tokens") == limit
            return StructuredResponse(content={"status": "ok"})

    adapter = AgentScopeStructuredModel(StructuredModel())
    output = await adapter.generate(
        ModelRequest(
            messages=request_with_history().messages, schema={"type": "object"}, max_tokens=limit
        )
    )

    assert output.data == {"status": "ok"}
