import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from threading import Event, Thread

import pytest

httpx = pytest.importorskip("httpx")

from smackend.outbound.client import AsyncClient, Client, Provider, ProviderFailure
from smackend.outbound.django import configure


def test_response_authentication_retry_and_redaction(monkeypatch, caplog):
    monkeypatch.setattr("smackend.outbound.client.sleep", lambda delay: None)
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-test-secret"
        if len(calls) == 1:
            return httpx.Response(503, content=b"private provider body")
        return httpx.Response(200, json={"count": 2})

    provider = Provider("https://catalog.example", headers={"Authorization": "Bearer synthetic-test-secret"})
    with Client(provider, transport=httpx.MockTransport(handle)) as client:
        reply = client.request("GET", "/objects", params={"query": "private-query"})
        assert reply.json(lambda value: int(value["count"])) == 2
    assert len(calls) == 2
    assert "synthetic-test-secret" not in repr(provider) + caplog.text
    assert "private provider body" not in caplog.text


def test_non_idempotent_requests_and_retry_after(monkeypatch):
    monkeypatch.setattr("smackend.outbound.client.sleep", lambda delay: None)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503, json={"details": "hidden"}) if len(calls) < 3 else httpx.Response(200)

    provider = Provider("https://payments.example")
    with Client(provider, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderFailure, match="provider_status"):
            client.request("POST", "/payments", json={"amount": 5})
        assert len(calls) == 1
        with pytest.raises(ValueError, match="idempotency key"):
            client.request("POST", "/payments", retry_non_idempotent=True)
        result = client.request("POST", "/payments", json={"amount": 5}, retry_non_idempotent=True,
                                idempotency_key="client-operation-17")
        assert result.status == 200
        assert [item.headers["Idempotency-Key"] for item in calls[1:]] == ["client-operation-17"] * 2
    with Client(provider, transport=httpx.MockTransport(
        lambda _: httpx.Response(429, headers={"Retry-After": "120"}),
    )) as client:
        with pytest.raises(ProviderFailure) as failure:
            client.request("GET", "/quota")
        assert failure.value.code == "rate_limited" and failure.value.retry_after == 120


@pytest.mark.parametrize("route", ["//elsewhere.example", "/../x", "/%2felsewhere", "/x%255c", "/x?hidden",
                                    "https://elsewhere.example/x", "/x#fragment"])
def test_unsafe_paths(route):
    with Client(Provider("https://api.example"), transport=httpx.MockTransport(
        lambda _: pytest.fail("an unsafe request reached the network"),
    )) as client:
        with pytest.raises(ValueError):
            client.request("GET", route)


def test_response_limits_timeout_and_no_redirect():
    def handler(request):
        match request.url.path:
            case "/large":
                return httpx.Response(200, content=b"abcdef")
            case "/timeout":
                raise httpx.ReadTimeout("secret detail")
            case "/bad-json":
                return httpx.Response(200, content=b"[bad")
            case "/redirect":
                return httpx.Response(302, headers={"Location": "https://untrusted.example"})
            case _:
                pytest.fail("Unexpected provider request")

    provider = Provider("https://api.example", max_response_bytes=5, attempts=1)
    with Client(provider, transport=httpx.MockTransport(handler)) as client:
        for route, code in (("/large", "response_too_large"), ("/timeout", "provider_timeout"),
                            ("/redirect", "provider_status")):
            with pytest.raises(ProviderFailure, match=code):
                client.request("GET", route)
        with pytest.raises(ProviderFailure, match="invalid_response"):
            client.request("GET", "/bad-json").json()


def test_async_cancellation_releases_admission_slot():
    async def scenario():
        entered = asyncio.Event()

        async def handle(request):
            if request.url.path == "/wait":
                entered.set()
                await asyncio.Future()
            return httpx.Response(200, json={"ok": True})

        provider = Provider("https://api.example", concurrency=1, admission_timeout=0.01)
        async with AsyncClient(provider, transport=httpx.MockTransport(handle)) as client:
            task = asyncio.create_task(client.request("GET", "/wait"))
            await entered.wait()
            with pytest.raises(ProviderFailure, match="provider_busy"):
                await client.request("GET", "/ok")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await client.request("GET", "/ok")).json() == {"ok": True}

    asyncio.run(scenario())


def test_configuration_requires_referenced_secrets():
    from django.core.exceptions import ImproperlyConfigured

    target = {"LOGGING": {"loggers": {}}}
    specification = '{"catalog":{"base_url":"https://api.example","credentials":{"Authorization":{"env":"CATALOG_TOKEN","prefix":"Bearer "}}}}'
    with pytest.raises(ImproperlyConfigured, match="CATALOG_TOKEN"):
        configure(target, {"SMACKEND_HTTP_PROVIDERS": specification}, production=True)
    configure(target, {"SMACKEND_HTTP_PROVIDERS": specification, "CATALOG_TOKEN": "synthetic"},
              production=True)
    assert target["SMACKEND_OUTBOUND_PROVIDERS"]["catalog"].headers["Authorization"] == "Bearer synthetic"


def test_sync_keepalive_over_real_loopback():
    if not os.environ.get("SMACKEND_SMOKE_DSN"):
        pytest.skip("Run with the integration environment to allow a local HTTP listener")
    peers = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            peers.append(self.client_address[1])
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            provider = Provider(f"http://127.0.0.1:{server.server_port}", allow_http=True)
            with Client(provider) as client:
                for _ in range(2):
                    assert client.request("GET", "/ready").body == b"ok"
            assert len(peers) == 2 and len(set(peers)) == 1
        finally:
            server.shutdown()
            server_thread.join(timeout=5)
