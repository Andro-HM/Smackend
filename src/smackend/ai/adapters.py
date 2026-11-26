"""Vendor wire formats. The caller supplies models, capabilities and HTTP origins."""

from base64 import b64decode, b64encode
from dataclasses import replace
import json
import re

from smackend.outbound.client import AsyncClient, ProviderFailure

from .contracts import AIError, Event, Part, Result, ToolCall, Usage


def encoded(part):
    return b64encode(part.data).decode("ascii")


def tokens(data, input_key, output_key):
    if input_key not in data and output_key not in data:
        return None
    return Usage(data.get(input_key, 0), data.get(output_key, 0))


def provider_error(exc, *, replay_429):
    codes = {"provider_timeout": 504, "provider_busy": 503, "rate_limited": 429}
    # The HTTP layer never retries these POSTs. An operator must opt in to
    # treating a provider's 429 contract as a definite admission rejection.
    safe = exc.code == "provider_busy" or (replay_429 and exc.status == 429)
    return AIError(exc.code, "The AI provider could not complete the request.", status=codes.get(exc.code, 502),
                   safe_to_retry=safe, retry_after=exc.retry_after)


async def sse_messages(chunks, *, complete=None):
    pending, fields = bytearray(), []
    finished = False
    async for chunk in chunks:
        pending.extend(chunk)
        while (index := pending.find(b"\n")) >= 0:
            line = bytes(pending[:index]).rstrip(b"\r")
            del pending[:index + 1]
            if line.startswith(b"data:"):
                fields.append(line[5:].lstrip(b" "))
            elif not line and fields:
                payload = b"\n".join(fields)
                fields.clear()
                if payload == b"[DONE]":
                    return
                message = json.loads(payload)
                if not isinstance(message, dict) or message.get("error") or message.get("type") == "error":
                    raise AIError("provider_stream_failed", "The AI provider ended the stream with an error.")
                yield message
                finished = finished or bool(complete and complete(message))
    if not finished:
        raise AIError("incomplete_stream", "The AI provider disconnected before completing the response.")


class WireAdapter:
    def __init__(self, provider, *, prefix, replay_429=False, transport=None):
        self.provider = replace(provider, attempts=1, headers=dict(provider.headers))
        self.prefix = prefix.rstrip("/")
        self.replay_429 = replay_429
        self.transport = transport

    def client(self):
        """Return a native outbound client for vendor-specific operations."""
        return AsyncClient(self.provider, transport=self.transport)

    async def call(self, operation, model, payload, *, client):
        try:
            path, body, options = self.build(operation, model, payload)
            response = await client.request("POST", path, json=body, **options)
            return self.parse(operation, response)
        except ProviderFailure as exc:
            raise provider_error(exc, replay_429=self.replay_429) from None
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise AIError("invalid_provider_response", "The AI provider returned an invalid response.") from None

    async def stream(self, model, prompt, *, client):
        try:
            path, body, options = self.build("stream", model, prompt)
            async with client.stream("POST", path, json=body, **options) as chunks:
                async for event in sse_messages(chunks, complete=self.stream_complete):
                    for result in self.parse_event(event):
                        yield result
        except ProviderFailure as exc:
            raise provider_error(exc, replay_429=self.replay_429) from None
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise AIError("invalid_provider_response", "The AI provider returned an invalid stream.") from None

    def stream_complete(self, event):
        return False


class OpenAIAdapter(WireAdapter):
    supported = frozenset({"text", "stream", "json", "tools", "image_input", "document_input", "audio_input",
                           "embeddings", "image", "speech"})

    def __init__(self, provider, *, prefix="/v1", **kwargs):
        super().__init__(provider, prefix=prefix, **kwargs)

    def build(self, operation, model, payload):
        if operation in {"text", "stream"}:
            messages = []
            for message in payload.messages:
                content = []
                for part in message.parts:
                    if part.kind == "text":
                        content.append({"type": "text", "text": part.data})
                    elif part.kind == "image":
                        content.append({"type": "image_url", "image_url": {"url": f"data:{part.mime};base64,{encoded(part)}"}})
                    elif part.kind == "document":
                        content.append({"type": "file", "file": {"filename": "input.pdf",
                                                                "file_data": f"data:{part.mime};base64,{encoded(part)}"}})
                    elif part.kind == "audio" and part.mime in {"audio/wav", "audio/mpeg"}:
                        content.append({"type": "input_audio", "input_audio": {
                            "data": encoded(part), "format": "wav" if part.mime == "audio/wav" else "mp3",
                        }})
                    else:
                        raise AIError("unsupported_input", "This adapter requires WAV or MP3 audio.", status=400)
                # Many compatible servers expect a string for text-only messages.
                value = "\n".join(item["text"] for item in content) if all(
                    item["type"] == "text" for item in content
                ) else content
                wire = {"role": message.role, "content": value}
                if message.calls:
                    wire["tool_calls"] = [{"type": "function", "id": call.id, "function": {
                        "name": call.name, "arguments": json.dumps(call.arguments),
                    }} for call in message.calls]
                if message.role == "tool":
                    wire["tool_call_id"] = message.tool_id
                messages.append(wire)
            body = {"model": model, "messages": messages, "max_completion_tokens": payload.max_tokens}
            if payload.schema is not None:
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "response", "schema": payload.schema, "strict": True,
                }}
            if payload.tools:
                body["tools"] = [{"type": "function", "function": {"name": tool.name, "description": tool.description,
                                                                    "parameters": tool.schema}} for tool in payload.tools]
            if operation == "stream":
                body.update(stream=True, stream_options={"include_usage": True})
            return self.prefix + "/chat/completions", body, {}
        paths = {"embeddings": "/embeddings", "image": "/images/generations", "speech": "/audio/speech"}
        return self.prefix + paths[operation], {"model": model, **payload}, {}

    def parse(self, operation, response):
        if operation == "speech":
            return Result(media=(Part("audio", response.body, "audio/mpeg"),))
        data = response.json()
        use = tokens(data.get("usage") or {}, "prompt_tokens", "completion_tokens")
        if operation == "embeddings":
            vectors = tuple(tuple(item["embedding"]) for item in sorted(data["data"], key=lambda item: item["index"]))
            return Result(vectors=vectors, usage=use, native=data)
        if operation == "image":
            media = tuple(Part("image", b64decode(item["b64_json"], validate=True), "image/png") for item in data["data"])
            return Result(media=media, usage=use, native=data)
        message = data["choices"][0]["message"]
        calls = tuple(ToolCall(item["id"], item["function"]["name"], json.loads(item["function"]["arguments"]))
                      for item in message.get("tool_calls") or ())
        if message.get("refusal"):
            raise AIError("provider_refused", "The AI provider refused the request.")
        return Result(text=message.get("content") or "", calls=calls, usage=use, native=data)

    def parse_event(self, data):
        for choice in data.get("choices", ()):
            delta = choice["delta"]
            if delta.get("refusal"):
                raise AIError("provider_refused", "The AI provider refused the request.")
            if delta.get("content"):
                yield Event("text", delta["content"])
            for fragment in delta.get("tool_calls") or ():
                yield Event("tool_delta", fragment)
        if use := tokens(data.get("usage") or {}, "prompt_tokens", "completion_tokens"):
            yield Event("usage", use)


class CompatibleAdapter(OpenAIAdapter):
    def build(self, operation, model, payload):
        path, body, options = super().build(operation, model, payload)
        if operation in {"text", "stream"}:
            body["max_tokens"] = body.pop("max_completion_tokens")
            body.pop("stream_options", None)
        return path, body, options


class AnthropicAdapter(WireAdapter):
    supported = frozenset({"text", "stream", "json", "tools", "image_input", "document_input"})

    def __init__(self, provider, *, prefix="/v1", **kwargs):
        super().__init__(provider, prefix=prefix, **kwargs)

    def build(self, operation, model, prompt):
        messages, system = [], []
        for message in prompt.messages:
            if message.role == "system":
                system.extend(part.data for part in message.parts)
                continue
            content = [{"type": "text", "text": part.data} if part.kind == "text" else {
                "type": part.kind, "source": {"type": "base64", "media_type": part.mime, "data": encoded(part)},
            } for part in message.parts]
            if message.role == "tool":
                content = [{"type": "tool_result", "tool_use_id": message.tool_id, "content": content}]
            content.extend({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                           for call in message.calls)
            messages.append({"role": "user" if message.role == "tool" else message.role, "content": content})
        body = {"model": model, "max_tokens": prompt.max_tokens, "messages": messages}
        if system:
            body["system"] = "\n".join(system)
        if prompt.schema is not None:
            body["output_config"] = {"format": {"type": "json_schema", "schema": prompt.schema}}
        if prompt.tools:
            body["tools"] = [{"name": tool.name, "description": tool.description, "input_schema": tool.schema}
                             for tool in prompt.tools]
        if operation == "stream":
            body["stream"] = True
        return self.prefix + "/messages", body, {}

    def parse(self, operation, response):
        data = response.json()
        content = data["content"]
        return Result(text="".join(item["text"] for item in content if item["type"] == "text"),
                      calls=tuple(ToolCall(item["id"], item["name"], item["input"])
                                  for item in content if item["type"] == "tool_use"),
                      usage=tokens(data.get("usage", {}), "input_tokens", "output_tokens"), native=data)

    def stream_complete(self, event):
        return event.get("type") == "message_stop"

    def parse_event(self, event):
        match event.get("type"):
            case "message_start":
                if use := tokens(event["message"].get("usage", {}), "input_tokens", "output_tokens"):
                    yield Event("usage", use)
            case "message_delta":
                if use := tokens(event.get("usage", {}), "input_tokens", "output_tokens"):
                    yield Event("usage", use)
            case "content_block_start":
                if event["content_block"]["type"] == "tool_use":
                    yield Event("tool_delta", {"index": event["index"], **event["content_block"]})
            case "content_block_delta":
                delta = event["delta"]
                if delta["type"] == "text_delta":
                    yield Event("text", delta["text"])
                if delta["type"] == "input_json_delta":
                    yield Event("tool_delta", {"index": event["index"], **delta})


class GeminiAdapter(WireAdapter):
    supported = frozenset({"text", "stream", "json", "tools", "image_input", "document_input", "audio_input",
                           "embeddings", "image", "speech"})

    def __init__(self, provider, *, prefix="/v1beta", **kwargs):
        super().__init__(provider, prefix=prefix, **kwargs)

    def build(self, operation, model, payload):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", model):
            raise AIError("invalid_model", "Use the provider's bare Gemini model identifier.", status=400)
        path = f"{self.prefix}/models/{model}"
        if operation == "embeddings":
            return path + ":batchEmbedContents", {"requests": [
                {"model": f"models/{model}", "content": {"parts": [{"text": text}]}} for text in payload["input"]
            ]}, {}
        if operation in {"image", "speech"}:
            config = {"responseModalities": ["IMAGE" if operation == "image" else "AUDIO"]}
            if operation == "speech":
                config["speechConfig"] = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": payload["voice"]}}}
            return path + ":generateContent", {
                "contents": [{"parts": [{"text": payload["input" if operation == "speech" else "prompt"]}]}],
                "generationConfig": config,
            }, {}
        system, contents = [], []
        for message in payload.messages:
            parts = [{"text": part.data} if part.kind == "text" else {
                "inlineData": {"mimeType": part.mime, "data": encoded(part)},
            } for part in message.parts]
            parts.extend({"functionCall": {"name": call.name, "args": call.arguments,
                                           **({"id": call.id} if call.id else {})}} for call in message.calls)
            if message.role == "tool":
                parts = [{"functionResponse": {"name": message.tool_name,
                                               "response": {"output": "\n".join(part.data for part in message.parts)},
                                               **({"id": message.tool_id} if message.tool_id else {})}}]
            if message.role == "system":
                system.extend(parts)
            else:
                contents.append({"role": "model" if message.role == "assistant" else "user", "parts": parts})
        body = {"contents": contents, "generationConfig": {"maxOutputTokens": payload.max_tokens}}
        if system:
            body["systemInstruction"] = {"parts": system}
        if payload.schema is not None:
            body["generationConfig"].update(responseMimeType="application/json", responseJsonSchema=payload.schema)
        if payload.tools:
            body["tools"] = [{"functionDeclarations": [{"name": item.name, "description": item.description,
                                                        "parameters": item.schema} for item in payload.tools]}]
        if operation == "stream":
            return path + ":streamGenerateContent", body, {"params": {"alt": "sse"}}
        return path + ":generateContent", body, {}

    def parse(self, operation, response):
        data = response.json()
        if operation == "embeddings":
            return Result(vectors=tuple(tuple(item["values"]) for item in data["embeddings"]), native=data)
        use = self.usage(data)
        parts = data["candidates"][0]["content"]["parts"]
        text, media, calls = [], [], []
        for part in parts:
            if part.get("thought"):
                continue
            if "text" in part:
                text.append(part["text"])
            if content := part.get("inlineData"):
                mime = content["mimeType"]
                media.append(Part("image" if mime.startswith("image/") else "audio",
                                  b64decode(content["data"], validate=True), mime))
            if call := part.get("functionCall"):
                calls.append(ToolCall(call.get("id", ""), call["name"], call["args"]))
        if operation in {"image", "speech"} and not media:
            raise AIError("invalid_provider_response", "The provider returned no media for this operation.")
        return Result(text="".join(text), media=tuple(media), calls=tuple(calls), usage=use, native=data)

    def stream_complete(self, event):
        return any(item.get("finishReason") for item in event.get("candidates", ()))

    def parse_event(self, event):
        if event.get("promptFeedback", {}).get("blockReason"):
            raise AIError("provider_refused", "The AI provider refused the request.")
        for candidate in event.get("candidates", ()):
            for part in candidate.get("content", {}).get("parts", ()):
                if part.get("text") and not part.get("thought"):
                    yield Event("text", part["text"])
                if "functionCall" in part:
                    yield Event("tool_delta", part["functionCall"])
        if use := self.usage(event):
            yield Event("usage", use)

    @staticmethod
    def usage(data):
        data = data.get("usageMetadata", {})
        if "totalTokenCount" in data:
            prompt = data.get("promptTokenCount", 0)
            return Usage(prompt, max(0, data["totalTokenCount"] - prompt))
        return tokens(data, "promptTokenCount", "candidatesTokenCount")
