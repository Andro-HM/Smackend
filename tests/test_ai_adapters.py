import asyncio
import json
from base64 import b64encode

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("jsonschema")

from smackend.ai.adapters import AnthropicAdapter, CompatibleAdapter, GeminiAdapter, OpenAIAdapter
from smackend.ai.contracts import AIError, Message, Part, Prompt, Tool, ToolCall, validate_schema
from smackend.outbound.client import Provider


PROMPT = Prompt((Message("user", (Part("text", "one"),)),
                 Message("assistant", (Part("text", "two"),)),
                 Message("user", (Part("text", "three"), Part("image", b"png", "image/png"),
                                  Part("document", b"pdf", "application/pdf")))),
                tools=(Tool("read", "Read an application record", {"type": "object"}),))


@pytest.mark.parametrize("adapter,endpoint,reply", [
    (OpenAIAdapter, "/v1/chat/completions", {
        "choices": [{"message": {"content": "answer", "tool_calls": [
            {"id": "1", "function": {"name": "read", "arguments": '{"id":3}'}}]}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }),
    (AnthropicAdapter, "/v1/messages", {
        "content": [{"type": "text", "text": "answer"},
                    {"type": "tool_use", "id": "1", "name": "read", "input": {"id": 3}}],
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }),
    (GeminiAdapter, "/v1beta/models/example:generateContent", {
        "candidates": [{"content": {"parts": [{"text": "answer"},
                                             {"functionCall": {"name": "read", "args": {"id": 3}}}]}}],
        "usageMetadata": {"promptTokenCount": 4, "totalTokenCount": 6},
    }),
])
def test_native_adapter_conformance(adapter, endpoint, reply):
    def handler(request):
        assert request.url.path == endpoint
        body = json.loads(request.content)
        assert len(body.get("messages") or body["contents"]) == 3
        assert body["tools"]
        assert "cG5n" in request.content.decode() and "cGRm" in request.content.decode()
        return httpx.Response(200, json=reply)

    async def run():
        configured = adapter(Provider("https://model.example"), transport=httpx.MockTransport(handler))
        async with configured.client() as client:
            result = await configured.call("text", "example", PROMPT, client=client)
        assert result.text == "answer" and result.calls[0].arguments == {"id": 3}
        assert result.usage.output_tokens == 2

    asyncio.run(run())


@pytest.mark.parametrize("adapter,events", [
    (OpenAIAdapter, [{"choices": [{"delta": {"content": "token"}}]}, "[DONE]"]),
    (AnthropicAdapter, [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "token"}},
                        {"type": "message_stop"}]),
    (GeminiAdapter, [{"candidates": [{"content": {"parts": [{"text": "token"}]}, "finishReason": "STOP"}]}]),
])
def test_stream_events(adapter, events):
    sse = "".join("data: " + (event if isinstance(event, str) else json.dumps(event)) + "\r\n\r\n"
                  for event in events)

    async def run():
        configured = adapter(Provider("https://model.example"), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text=sse),
        ))
        async with configured.client() as client:
            texts = [event.data async for event in configured.stream("example", PROMPT, client=client)
                     if event.kind == "text"]
        assert texts == ["token"]

    asyncio.run(run())


def test_no_retry_after_ambiguous_failure():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("private provider detail")

    async def run():
        adapter = OpenAIAdapter(Provider("https://model.example"), transport=httpx.MockTransport(handle))
        async with adapter.client() as client:
            with pytest.raises(AIError) as failure:
                await adapter.call("text", "example", PROMPT, client=client)
        assert failure.value.status == 504 and not failure.value.safe_to_retry
        assert len(calls) == 1 and "private provider detail" not in str(failure.value)

    asyncio.run(run())


def test_structured_schema_validation_offline():
    schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
    validate_schema(schema, {"answer": "ok"})
    with pytest.raises(AIError):
        validate_schema(schema, {"answer": 3})
    with pytest.raises(AIError):
        validate_schema({"$ref": "https://example.invalid/schema"}, None)


def test_embeddings_image_audio_and_tool_continuation():
    result = b64encode(b"simulated media").decode()

    def handler(request):
        payload = json.loads(request.content)
        match request.url.path:
            case "/local/embeddings":
                assert payload["input"] == ["one"]
                return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
            case "/local/images/generations":
                return httpx.Response(200, json={"data": [{"b64_json": result}]})
            case "/local/audio/speech":
                assert payload["voice"] == "selected-voice"
                return httpx.Response(200, content=b"synthetic audio")
            case "/local/chat/completions":
                assert "max_tokens" in payload and payload["messages"][1]["tool_calls"][0]["id"] == "q1"
                assert payload["messages"][2]["tool_call_id"] == "q1"
                assert payload["messages"][0]["content"][1]["input_audio"]["format"] == "wav"
                return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})
        raise AssertionError("Unexpected operation")

    async def verify():
        model = CompatibleAdapter(Provider("http://127.0.0.1:9123", allow_http=True), prefix="/local",
                                  transport=httpx.MockTransport(handler))
        async with model.client() as client:
            result1 = await model.call("embeddings", "fixture", {"input": ["one"]}, client=client)
            result2 = await model.call("image", "fixture", {"prompt": "image"}, client=client)
            result3 = await model.call("speech", "fixture", {"input": "audio", "voice": "selected-voice"}, client=client)
            prompt = Prompt((
                Message("user", (Part("text", "check"), Part("audio", b"wave", "audio/wav"))),
                Message("assistant", (), (ToolCall("q1", "check", {}),)),
                Message("tool", (Part("text", "checked"),), tool_id="q1", tool_name="check"),
            ))
            result4 = await model.call("text", "fixture", prompt, client=client)
        assert result1.vectors == ((0.1, 0.2),) and result2.media[0].data == b"simulated media"
        assert result3.media[0].data == b"synthetic audio" and result4.text == "done"

    asyncio.run(verify())
