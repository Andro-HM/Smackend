"""Short simultaneous-load diagnostic with independently scheduled HTTP and PCM traffic."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import gzip
import json
from pathlib import Path
from random import Random
from struct import pack
from time import monotonic
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect

from load import MIXES, percentiles, request


async def exercise(args, actors):
    if len(actors) < 150 or args.seconds < 1 or args.seconds > 540:
        raise ValueError("Use at least 150 seeded accounts and a diagnostic window of 1–540 seconds.")
    samples = []
    missing = {"crud": 0, "login": 0}
    engaged = set()
    active = set()
    occupancy = []
    randomizer = Random(40)
    operations = {name: MIXES["crud"][name] for name in ("read", "write", "aggregate")}

    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(httpx.AsyncClient(
            base_url=args.url, trust_env=False, timeout=12, limits=httpx.Limits(max_connections=120),
        ))
        url = args.url.replace("http", "ws", 1) + "/ws/voice/"

        async def open_voice(actor):
            socket = await stack.enter_async_context(connect(
                url, origin=args.url, additional_headers={"Cookie": f"sessionid={actor['session']}"},
                open_timeout=10, close_timeout=1,
            ))
            await socket.send(json.dumps({"type": "start", "key": str(uuid4())}))
            event = json.loads(await asyncio.wait_for(socket.recv(), 5))
            if event["type"] != "voice_ready":
                raise RuntimeError("A voice socket did not enter the relay.")
            return socket

        async with asyncio.TaskGroup() as group:
            openings = [group.create_task(open_voice(actor)) for actor in actors[:50]]
        sockets = [opening.result() for opening in openings]
        start = monotonic() + 0.25
        stop = start + args.seconds

        async def use_voice(index, socket):
            sent = []
            rtt = []
            late = []
            expected = args.seconds * 50
            active.add(index)

            async def send():
                for sequence in range(expected):
                    target = start + sequence * 0.02
                    await asyncio.sleep(max(0, target - monotonic()))
                    now = monotonic()
                    sent.append(now)
                    late.append((now - target) * 1000)
                    await socket.send(pack("!HH", index, sequence % 65536) * 160)

            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(send())
                    for sequence in range(expected):
                        data = await asyncio.wait_for(socket.recv(), 3)
                        if data != pack("!HH", index, sequence % 65536) * 160:
                            raise ValueError("Audio was corrupted or delivered out of order.")
                        rtt.append((monotonic() - sent[sequence]) * 1000)
                await asyncio.sleep(max(0, stop - monotonic()))
            except ExceptionGroup:
                pass
            finally:
                active.discard(index)
            return len(rtt), expected, rtt, late

        async def emit(actor, operation, sequence, target):
            try:
                status, valid, _, extra = await request(client, args.url, actor, operation, sequence)
            except Exception as exc:
                status, valid, extra = 0, False, {"error": type(exc).__name__}
            done = monotonic()
            samples.append({"at": target - start, "op": operation, "status": status, "valid": valid,
                            "ms": (done - target) * 1000, "in_window": done <= stop, **extra})
            engaged.discard(actor["index"])

        async def pace(group, name, rate, participants):
            for sequence in range(args.seconds * rate):
                target = start + sequence / rate
                await asyncio.sleep(max(0, target - monotonic()))
                actor = participants[sequence % len(participants)]
                if actor["index"] in engaged or monotonic() - target > 0.25:
                    missing[name] += 1
                    continue
                operation = "login" if name == "login" else randomizer.choices(
                    list(operations), weights=list(operations.values()),
                )[0]
                engaged.add(actor["index"])
                group.create_task(emit(actor, operation, sequence, target))

        async def observe():
            await asyncio.sleep(max(0, start - monotonic()))
            while monotonic() < stop:
                occupancy.append(len(active))
                await asyncio.sleep(0.05)

        async with asyncio.TaskGroup() as group:
            voice = [group.create_task(use_voice(index, socket)) for index, socket in enumerate(sockets)]
            group.create_task(observe())
            group.create_task(pace(group, "crud", 25, actors[50:100]))
            group.create_task(pace(group, "login", 5, actors[100:]))

    with gzip.open(args.raw, "wt", encoding="utf-8") as output:
        for sample in samples:
            output.write(json.dumps(sample) + "\n")
    results = [task.result() for task in voice]
    groups = {}
    for operation in sorted({sample["op"] for sample in samples}):
        subset = [sample for sample in samples if sample["op"] == operation]
        groups[operation] = {
            "count": len(subset),
            "valid": sum(sample["valid"] for sample in subset),
            "valid_in_window": sum(sample["valid"] and sample["in_window"] for sample in subset),
            "latency_ms": percentiles([sample["ms"] for sample in subset]),
        }
    in_window = sum(sample["in_window"] for sample in samples)
    return {
        "seconds": args.seconds, "window_monotonic": [start, stop],
        "offered": args.seconds * 30, "missed_by_workload": missing, "late_or_busy": sum(missing.values()),
        "completed": len(samples), "valid": sum(sample["valid"] for sample in samples),
        "completed_rps": in_window / args.seconds, "completed_in_window": in_window,
        "completed_in_drain": len(samples) - in_window, "groups": groups,
        "voice_min_sampled_active": min(occupancy, default=0),
        "voice_frames_received": sum(item[0] for item in results),
        "voice_frames_expected": sum(item[1] for item in results),
        "voice_rtt_ms": percentiles([value for item in results for value in item[2]]),
        "voice_frame_lateness_ms": percentiles([value for item in results for value in item[3]]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--identities", type=Path, required=True)
    parser.add_argument("--seconds", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(exercise(args, json.loads(args.identities.read_text(encoding="utf-8"))))))
