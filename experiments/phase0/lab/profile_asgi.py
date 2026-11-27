"""Optional timing probes for the isolated benchmark. No SQL or credentials are emitted."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from functools import wraps
import json
import os
from time import perf_counter, thread_time

from asgiref.sync import SyncToAsync

request_profile = ContextVar("phase0_request_profile", default=None)
submit = ThreadPoolExecutor.submit
bridge_call = SyncToAsync.__call__


def measured_submit(executor, fn, /, *args, **kwargs):
    sample = request_profile.get()
    if sample is None:
        return submit(executor, fn, *args, **kwargs)
    scheduled = perf_counter()

    def run():
        started = perf_counter()
        cpu_start = thread_time()
        try:
            return fn(*args, **kwargs)
        finally:
            sample["sync_cpu"] += thread_time() - cpu_start
            sample["thread_calls"] += 1
            sample["thread_wait"] += started - scheduled
            sample["thread_run"] += perf_counter() - started

    return submit(executor, run)


@wraps(bridge_call)
async def measured_bridge(bridge, *args, **kwargs):
    sample = request_profile.get()
    if sample is None:
        return await bridge_call(bridge, *args, **kwargs)
    started = perf_counter()
    try:
        return await bridge_call(bridge, *args, **kwargs)
    finally:
        sample["bridge"] += perf_counter() - started


def instrument(app):
    if os.getenv("QUALIFICATION_WIRE_TIMING") == "1":
        from uvicorn.protocols.http.h11_impl import RequestResponseCycle

        original_init = RequestResponseCycle.__init__

        @wraps(original_init)
        def measured_init(cycle, *args, **kwargs):
            original_init(cycle, *args, **kwargs)
            cycle.scope["qualification_headers_parsed_at"] = perf_counter()

        RequestResponseCycle.__init__ = measured_init
    ThreadPoolExecutor.submit = measured_submit
    SyncToAsync.__call__ = measured_bridge
    if os.getenv("QUALIFICATION_SOCKET_PROFILE") == "1":
        from channels import consumer

        cleanup = consumer.aclose_old_connections

        async def measured_cleanup():
            sample = request_profile.get()
            if sample is None:
                return await cleanup()
            started = perf_counter()
            try:
                return await cleanup()
            finally:
                sample["cleanup_calls"] = sample.get("cleanup_calls", 0) + 1
                sample["cleanup_seconds"] = sample.get("cleanup_seconds", 0) + perf_counter() - started

        consumer.aclose_old_connections = measured_cleanup

    lag = 0.0
    heartbeat = None

    async def watch_loop():
        nonlocal lag
        while True:
            scheduled = perf_counter() + 0.025
            await asyncio.sleep(0.025)
            lag = max(0, perf_counter() - scheduled)

    async def profile(scope, receive, send):
        if scope["type"] == "websocket" and os.getenv("QUALIFICATION_SOCKET_PROFILE") == "1":
            start = perf_counter()
            sample = dict(db=0.0, checkout=0.0, lease=0.0, thread_wait=0.0,
                          thread_run=0.0, thread_calls=0, sync_cpu=0.0, bridge=0.0, queries={})
            token = request_profile.set(sample)
            try:
                return await app(scope, receive, send)
            finally:
                request_profile.reset(token)
                # One record at disconnect, after all message handling.
                print("QUALIFICATION_SOCKET " + json.dumps({
                    "pid": os.getpid(), "seconds": perf_counter() - start, **sample,
                }), flush=True)
        nonlocal heartbeat
        if scope["type"] != "http":
            return await app(scope, receive, send)
        if heartbeat is None:
            heartbeat = asyncio.create_task(watch_loop())
        start = perf_counter()
        sample = dict(db=0.0, checkout=0.0, lease=0.0, provider=0.0, thread_wait=0.0,
                      thread_run=0.0, thread_calls=0, sync_cpu=0.0, bridge=0.0)
        sample["queries"] = {}
        token = request_profile.set(sample)

        async def send_profile(message):
            if message["type"] == "http.response.start":
                timing = {
                    key: round(value * 1000, 3)
                    for key, value in sample.items()
                    if key not in {"queries", "thread_calls", "lease_started", "pool_available", "pool_waiting"}
                }
                if "lease_started" in sample:
                    timing["lease"] += round((perf_counter() - sample["lease_started"]) * 1000, 3)
                if parsed_at := scope.get("qualification_headers_parsed_at"):
                    timing["server_dispatch_ms"] = round((start - parsed_at) * 1000, 3)
                timing.update({key: sample[key] for key in ("pool_available", "pool_waiting") if key in sample})
                timing.update(
                    app=round((perf_counter() - start) * 1000, 3),
                    lag=round(lag * 1000, 3),
                    thread_calls=sample["thread_calls"],
                    queries=sample["queries"],
                    pid=os.getpid(),
                )
                message["headers"] = [*message["headers"], (b"x-phase0-profile", json.dumps(timing, separators=(",", ":")).encode())]
            await send(message)

        try:
            return await app(scope, receive, send_profile)
        finally:
            request_profile.reset(token)

    return profile
