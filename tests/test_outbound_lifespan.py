import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from smackend.outbound import asgi
from smackend.outbound.client import AsyncClient, Provider, ProviderFailure


def test_worker_lifespan_credential_boundaries_and_cancellation():
    async def scenario():
        config = Provider("https://synthetic.invalid", headers={"X-Key": "first"},
                          concurrency=1, admission_timeout=0.01)
        other = Provider("https://synthetic.invalid", headers={"X-Key": "other"})
        variant = Provider("https://synthetic.invalid", headers={"X-Key": "variant"}, concurrency=1)
        entered = asyncio.Event()
        clients = []

        async def upstream(request):
            assert "cookie" not in request.headers
            if request.url.path == "/block":
                entered.set()
                await asyncio.Future()
            if request.url.path == "/fail":
                raise httpx.ConnectError("simulated failure")
            return httpx.Response(200, headers={"Set-Cookie": "upstream=private; Path=/"},
                                  text=request.headers["X-Key"])

        def make_client(config, **kwargs):
            client = AsyncClient(config, transport=httpx.MockTransport(upstream), **kwargs)
            clients.append(client)
            return client

        async def application(scope, receive, send):
            async with AsyncExitStack() as stack:
                first = await stack.enter_async_context(asgi.outbound_client("first"))
                second = await stack.enter_async_context(asgi.outbound_client("other"))
                ai = asgi.ai_clients()
                assert ai["one"] is ai["two"]
                assert (await first.request("GET", "/")).body == b"first"
                assert (await second.request("GET", "/")).body == b"other"
                assert (await ai["one"].request("GET", "/")).body == b"variant"
                pending = asyncio.create_task(first.request("GET", "/block"))
                try:
                    await entered.wait()
                    with pytest.raises(ProviderFailure, match="provider_busy"):
                        await ai["one"].request("GET", "/")
                finally:
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                with pytest.raises(ProviderFailure, match="provider_unavailable"):
                    await first.request("GET", "/fail")
                async with first.stream("GET", "/") as response:
                    assert b"".join([chunk async for chunk in response]) == b"first"

        application.application_mapping = {"http": application, "websocket": application}
        config_settings = SimpleNamespace(SMACKEND_OUTBOUND_PROVIDERS={"first": config, "other": other},
                                          SMACKEND_AI_HTTP_CONFIG={"one": ("first", variant),
                                                                   "two": ("first", variant)})
        messages = asyncio.Queue()
        responses = asyncio.Queue()
        state = {}
        with (
            patch("smackend.outbound.django.settings", config_settings),
            patch("django.conf.settings", config_settings),
            patch.object(asgi, "AsyncClient", make_client),
        ):
            wrapped = asgi.with_outbound(application)
            assert wrapped.application_mapping is application.application_mapping
            task = asyncio.create_task(wrapped({"type": "lifespan", "state": state},
                                               messages.get, responses.put))
            try:
                await messages.put({"type": "lifespan.startup"})
                assert (await responses.get())["type"] == "lifespan.startup.complete"
                await wrapped({"type": "http", "state": state.copy()}, None, None)
                assert len(clients) == 3
            finally:
                await messages.put({"type": "lifespan.shutdown"})
                await task
            assert (await responses.get())["type"] == "lifespan.shutdown.complete"
            assert not state
            assert all(client._client.is_closed for client in clients)

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_worker_lifespan_closes_clients_when_initialization_fails():
    async def scenario():
        config = Provider("https://synthetic.invalid")
        first = AsyncClient(config)
        state = {}
        events = []

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            events.append(message)

        config_settings = SimpleNamespace(SMACKEND_OUTBOUND_PROVIDERS={"first": config, "second": config})
        with patch("django.conf.settings", config_settings), patch.object(
            asgi, "AsyncClient", side_effect=[first, RuntimeError("sensitive startup detail")],
        ):
            await asgi.with_outbound(lambda *_: None)({"type": "lifespan", "state": state}, receive, send)

        assert events == [{"type": "lifespan.startup.failed", "message": "Smackend outbound lifecycle failed."}]
        assert first._client.is_closed
        assert not state

    asyncio.run(scenario())
