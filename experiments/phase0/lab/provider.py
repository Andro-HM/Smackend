import asyncio
from time import perf_counter
from typing import Literal

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from ninja import Router, Schema
from ninja.errors import HttpError

from .profile_asgi import request_profile

router = Router()


class ProviderOutput(Schema):
    results: list[str]


class BadProviderResponse(ValueError):
    pass


@sync_to_async
def release_auth_connection():
    # Resolve the Django connection on the same thread as its session queries.
    connection.close()


@sync_to_async
def resolve_and_release(request):
    # Use Django's normal synchronous session resolver in a single thread hop.
    user = request.user
    try:
        return user if user.is_authenticated else None
    finally:
        connection.close()


async def session_for_safe_get(request):
    # This read-only endpoint never changes state. Use django_auth for writes.
    if settings.PHASE0_PROVIDER_RELEASE_DB == "sync":
        return await resolve_and_release(request)
    user = await request.auser()
    if settings.PHASE0_PROVIDER_RELEASE_DB == "1":
        # Authentication is complete. The provider handler has no DB work or transaction.
        await release_auth_connection()
    return user if user.is_authenticated else None


async def fetch_result(client, index, mode):
    response = await client.get("/sample", params={"index": index, "mode": mode})
    response.raise_for_status()
    try:
        value = response.json()["value"]
    except (ValueError, TypeError, KeyError) as exc:
        raise BadProviderResponse("Invalid provider response") from exc
    if not isinstance(value, str):
        raise BadProviderResponse("Invalid provider response")
    return value


@router.get("", auth=session_for_safe_get, response=ProviderOutput)
async def provider_sample(request, mode: Literal["ok", "slow", "error", "malformed"] = "ok"):
    started = perf_counter()
    try:
        async with asyncio.timeout(0.75):
            # The independent calls run together and are cancelled if a peer fails.
            async with httpx.AsyncClient(
                base_url=settings.PHASE0_PROVIDER_URL,
                timeout=0.4,
                trust_env=False,
                # Settings only allow plain HTTP loopback. Avoid building a TLS
                # certificate store on the event loop for each local-only client.
                verify=False,
            ) as client:
                async with asyncio.TaskGroup() as tasks:
                    calls = [tasks.create_task(fetch_result(client, index, mode)) for index in range(3)]
        return {"results": [task.result() for task in calls]}
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise HttpError(504, "Provider timed out") from exc
    except ExceptionGroup as exc:
        timeouts, _ = exc.split(httpx.TimeoutException)
        if timeouts:
            raise HttpError(504, "Provider timed out") from exc
        _, unexpected = exc.split((httpx.HTTPError, BadProviderResponse))
        if unexpected:
            raise unexpected
        raise HttpError(502, "Provider returned an unsuccessful response") from exc
    finally:
        sample = request_profile.get()
        if sample is not None:
            sample["provider"] = perf_counter() - started
