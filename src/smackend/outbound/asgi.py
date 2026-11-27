"""Manage configured HTTP connections in the event loop that serves requests."""

import asyncio
from contextlib import AsyncExitStack, nullcontext
from contextvars import ContextVar
from functools import wraps

from .client import AsyncClient


_current = ContextVar("smackend_outbound", default=None)


def outbound_client(name):
    """Context manager borrowing a worker client, or owning one outside ASGI lifespan."""
    from .django import provider

    worker = _current.get()
    return nullcontext(worker[0][name]) if worker is not None else AsyncClient(provider(name))


def ai_clients():
    worker = _current.get()
    return worker[1].copy() if worker is not None else {}


def with_outbound(application):
    """Wrap the completed Django/Channels app. Serve with ASGI lifespan enabled."""
    from django.conf import settings

    state_key = "smackend.outbound"

    @wraps(application)
    async def handle(scope, receive, send):
        if scope["type"] != "lifespan":
            token = _current.set(scope.get("state", {}).get(state_key))
            try:
                return await application(scope, receive, send)
            finally:
                _current.reset(token)

        if (await receive())["type"] != "lifespan.startup":
            raise RuntimeError("Expected ASGI lifespan startup.")
        state = scope.get("state")
        stage = "startup"
        try:
            if state is None:
                raise RuntimeError("The ASGI server must support lifespan state.")
            async with AsyncExitStack() as stack:
                providers = settings.SMACKEND_OUTBOUND_PROVIDERS
                slots = {name: asyncio.Semaphore(config.concurrency) for name, config in providers.items()}
                clients = {}
                variants = {}
                for name, config in providers.items():
                    client = await stack.enter_async_context(AsyncClient(config, _shared_slots=slots[name]))
                    clients[name] = client
                    variants[name] = [(config, client)]
                models = {}
                for alias, (name, config) in getattr(settings, "SMACKEND_AI_HTTP_CONFIG", {}).items():
                    for existing, client in variants[name]:
                        if config == existing:
                            break
                    else:
                        client = await stack.enter_async_context(AsyncClient(config, _shared_slots=slots[name]))
                        variants[name].append((config, client))
                    models[alias] = client
                state[state_key] = (clients, models)
                await send({"type": "lifespan.startup.complete"})
                stage = "shutdown"
                if (await receive())["type"] != "lifespan.shutdown":
                    raise RuntimeError("Expected ASGI lifespan shutdown.")
                state.pop(state_key, None)
            await send({"type": "lifespan.shutdown.complete"})
        except Exception:
            await send({"type": f"lifespan.{stage}.failed",
                        "message": "Smackend outbound lifecycle failed."})
        finally:
            if state is not None:
                state.pop(state_key, None)

    return handle
