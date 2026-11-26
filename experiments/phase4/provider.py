"""Loopback-only fixed-delay provider for qualification."""

import asyncio
import json


async def application(scope, receive, send):
    path = scope["path"]
    if scope["type"] == "websocket":
        await receive()
        if path != "/ws/voice/":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "websocket.accept"})
        await receive()
        await send({"type": "websocket.send", "text": '{"type":"voice_ready","sample_rate":16000}'})
        while (message := await receive())["type"] == "websocket.receive":
            if "bytes" in message:
                await send({"type": "websocket.send", "bytes": message["bytes"]})
        return
    if path.startswith("/wait/"):
        await asyncio.sleep(0.1)
    if path == "/v1/chat/completions":
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        prompt = json.loads(body)
        await asyncio.sleep(0.05)
        usage = {"prompt_tokens": 15, "completion_tokens": 4}
        if prompt.get("stream"):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            lengthy = prompt["messages"][-1]["content"] == "Stream 100 qualification events"
            chunks = (f"{i:03d} " for i in range(100)) if lengthy else ("synthetic ", "provider ", "response")
            for value in chunks:
                await asyncio.sleep(0.05)
                chunk = {"choices": [{"delta": {"content": value}}]}
                await send({"type": "http.response.body", "body": b"data: " + json.dumps(chunk).encode() + b"\n\n",
                            "more_body": True})
            if lengthy:
                usage["completion_tokens"] = 100
            chunk = {"choices": [], "usage": usage}
            await send({"type": "http.response.body",
                        "body": b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"})
            return
        value = {"choices": [{"message": {"content": "synthetic provider response"}}], "usage": usage}
    else:
        value = {"ready": True}
        if path.startswith("/wait/"):
            value["loopback_connection"] = scope["client"][1]
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(value).encode()})
