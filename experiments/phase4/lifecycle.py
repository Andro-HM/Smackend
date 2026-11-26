"""Qualification application's explicit outbound connection-pool lifecycle."""

from django.conf import settings

from smackend.outbound.client import AsyncClient
from smackend.outbound.django import provider


def with_outbound(application):
    async def handle(scope, receive, send):
        if scope["type"] != "lifespan":
            return await application(scope, receive, send)
        startup = await receive()
        if startup["type"] != "lifespan.startup":
            raise RuntimeError("An ASGI lifespan startup was expected.")
        try:
            async with AsyncClient(provider("synthetic")) as client:
                settings.QUALIFICATION_OUTBOUND_CLIENT = client
                await send({"type": "lifespan.startup.complete"})
                shutdown = await receive()
                if shutdown["type"] != "lifespan.shutdown":
                    raise RuntimeError("An ASGI lifespan shutdown was expected.")
        finally:
            settings.QUALIFICATION_OUTBOUND_CLIENT = None
        await send({"type": "lifespan.shutdown.complete"})

    return handle
