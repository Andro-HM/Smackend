"""Independent loopback ASGI server with deterministic synthetic responses."""

import asyncio
import json
from urllib.parse import parse_qs


async def application(scope, receive, send):
    query = parse_qs(scope["query_string"].decode("ascii"))
    mode = query.get("mode", ["ok"])[0]
    index = query.get("index", ["0"])[0]
    await asyncio.sleep(1 if mode == "slow" else 0.1)
    status = 503 if mode == "error" else 200
    body = b"invalid json" if mode == "malformed" else json.dumps({"value": f"sample-{index}"}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})
