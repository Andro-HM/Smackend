"""Deterministic offline adapter used by generated examples and tests."""

from contextlib import nullcontext
import json

from .contracts import Event, Part, Result, ToolCall, Usage


class MockAdapter:
    supported = frozenset({"text", "stream", "json", "tools", "image_input", "document_input", "audio_input",
                           "embeddings", "speech"})

    def client(self):
        return nullcontext()

    async def call(self, operation, model, payload, *, client):
        if operation == "embeddings":
            return Result(vectors=tuple((float(len(text)), 1.0) for text in payload["input"]), usage=Usage(2, 0))
        if operation == "speech":
            return Result(media=(Part("audio", b"\0" * 640, "audio/pcm"),), usage=Usage(3, 4))
        if payload.tools:
            return Result(calls=(ToolCall("offline-tool-call", payload.tools[0].name, {}),), usage=Usage(3, 4))
        count = sum(part.kind != "text" for message in payload.messages for part in message.parts)
        last = next((part.data for part in reversed(payload.messages[-1].parts) if part.kind == "text"), "")
        text = f"Offline response: {last}" + (f" [{count} attachments]" if count else "")
        if payload.schema is not None:
            text = json.dumps({"answer": text})
        return Result(text=text, usage=Usage(3, 4))

    async def stream(self, model, prompt, *, client):
        result = await self.call("text", model, prompt, client=client)
        if result.calls:
            for call in result.calls:
                yield Event("tool_delta", {"id": call.id, "name": call.name, "arguments": call.arguments})
        else:
            for start in range(0, len(result.text), 12):
                yield Event("text", result.text[start:start + 12])
        yield Event("usage", result.usage)
