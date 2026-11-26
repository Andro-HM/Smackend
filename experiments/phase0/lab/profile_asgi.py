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
    ThreadPoolExecutor.submit = measured_submit
    SyncToAsync.__call__ = measured_bridge
    lag = 0.0
    heartbeat = None

    async def watch_loop():
        nonlocal lag
        while True:
            scheduled = perf_counter() + 0.025
            await asyncio.sleep(0.025)
            lag = max(0, perf_counter() - scheduled)

    async def profile(scope, receive, send):
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
                    if key not in {"queries", "thread_calls", "lease_started"}
                }
                if "lease_started" in sample:
                    timing["lease"] += round((perf_counter() - sample["lease_started"]) * 1000, 3)
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
