"""Paced synthetic clients. Raw rows contain timings and outcomes only."""

import argparse
import asyncio
import gzip
import json
import math
from pathlib import Path
import random
from statistics import median
from struct import pack, unpack_from
from time import monotonic
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect


MIXES = {
    "crud": {"read": 60, "write": 20, "aggregate": 15, "session": 5},
    "mixed": {"read": 45, "write": 15, "aggregate": 10, "session": 5, "external": 10,
              "upload": 2, "download": 5, "ai": 5, "stream": 3},
    "mixed-realtime": {"read": 28, "write": 10, "aggregate": 7, "login": 5, "ai": 8,
                       "stream": 5, "external": 7, "voice-duplex": 30},
}
TEST_PASSWORD = "Fixture-only!Smackend-phase-four-275"
LONG_STREAM_INSTRUCTION = "Stream 100 qualification events"


def percentiles(values):
    if not values:
        return {}
    ordered = sorted(values)
    return {f"p{p}": round(ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)], 3)
            for p in (50, 95, 99)}


async def request(client, host, who, operation, iteration):
    cookie = f"sessionid={who['session']}; csrftoken={who['csrf']}"
    headers = {"Cookie": cookie, "X-CSRFToken": who["csrf"], "Origin": host}
    if operation in {"voice-direct", "voice-duplex"}:
        direct = operation == "voice-direct"
        async with connect(host.replace("http", "ws", 1) + "/ws/voice/", origin=host,
                           additional_headers=None if direct else {"Cookie": cookie},
                           open_timeout=10, close_timeout=1) as socket:
            await socket.send(json.dumps({"type": "start", "key": str(uuid4())}))
            if json.loads(await asyncio.wait_for(socket.recv(), 5))["type"] != "voice_ready":
                return 101, False, None, {"frames_received": 0, "frames_expected": 100}
            return await duplex(socket, who["index"])
    if operation in {"echo", "voice"}:
        endpoint = "/ws/voice/" if operation == "voice" else "/ws/realtime/"
        async with connect(host.replace("http", "ws", 1) + endpoint, origin=host,
                           additional_headers={"Cookie": cookie}, open_timeout=10, close_timeout=1) as socket:
            if operation == "voice":
                await socket.send(json.dumps({"type": "start", "key": str(uuid4())}))
                if json.loads(await asyncio.wait_for(socket.recv(), 5))["type"] != "voice_ready":
                    return 500, False, None, {}
                payload = bytes([who["index"] % 251, iteration % 251]) * 320
            else:
                payload = json.dumps({"type": "echo", "text": f"fixture-{who['index']}-{iteration}"})
            elapsed = []
            start = monotonic()
            for n in range(25 if operation == "voice" else 4):
                if n:
                    await asyncio.sleep(max(0, start + n * (0.02 if operation == "voice" else 0.2) - monotonic()))
                sent = monotonic()
                await socket.send(payload)
                answer = await asyncio.wait_for(socket.recv(), 3)
                if operation == "echo" and isinstance(answer, str):
                    answer = json.dumps(json.loads(answer))
                if answer != payload:
                    return 101, False, None, {}
                elapsed.append((monotonic() - sent) * 1000)
            return 101, True, None, {"rtt_ms": elapsed}
    if operation in {"login", "signup"}:
        initial = await client.get("/identity/browser/v1/auth/session", headers={"Cookie": ""})
        token = initial.cookies.get("csrftoken")
        if not token:
            return initial.status_code, False, None, {}
        username = f"fixture{who['index']:04d}" if operation == "login" else "signup_" + uuid4().hex[:16]
        response = await client.post(f"/identity/browser/v1/auth/{operation}",
                                     json={"username": username, "password": TEST_PASSWORD},
                                     headers={"Cookie": "csrftoken=" + token, "X-CSRFToken": token, "Origin": host})
        return response.status_code, response.status_code == 200 and bool(response.cookies.get("sessionid")), None, {}
    if operation == "control":
        response = await client.get("/")
        return response.status_code, response.status_code == 200, None, {}
    if operation == "create-delete":
        path = f"/api/bench/records/{100000 + iteration}"
        created = await client.post(path, json={"amount": iteration + 2}, headers=headers)
        if created.status_code != 201 or created.json().get("amount") != iteration + 2:
            return created.status_code, False, None, {}
        deleted = await client.delete(path, headers=headers)
        return deleted.status_code, deleted.status_code == 204, created.headers.get("x-phase0-profile"), {}
    kwargs = {}
    method = "GET"
    paths = {"read": "/api/bench/records", "aggregate": "/api/bench/total",
             "session": "/api/account/profile", "external": "/api/bench/external",
             "write": "/api/bench/records/0", "download": f"/api/files/{who['file']}/download",
             "upload": f"/api/files/{who['file']}/replace", "ai": "/api/ai/chat", "stream": "/api/ai/stream",
             "stream-long": "/api/ai/stream"}
    if operation == "write":
        method, kwargs = "PUT", {"json": {"amount": iteration + 2}}
    if operation == "upload":
        method = "POST"
        payload = (f"Owned text fixture {who['index']:04d}\n".encode() + b"x" * 4072)
        kwargs = {"files": {"file": ("fixture.txt", payload, "text/plain")}}
    if operation in {"ai", "stream", "stream-long"}:
        text = LONG_STREAM_INSTRUCTION if operation == "stream-long" else "Test response"
        method, kwargs = "POST", {"json": {"idempotency_key": str(uuid4()),
                                          "messages": [{"role": "user", "text": text}]}}
    if operation in {"stream", "stream-long"}:
        async with client.stream(method, paths[operation], headers=headers, **kwargs) as response:
            profile = response.headers.get("x-phase0-profile")
            events = []
            first = None
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    first = first or monotonic()
                    events.append((monotonic(), json.loads(line[6:])))
            valid = response.status_code == 200 and bool(events) and events[-1][1].get("done") is True
            expected = ("".join(f"{i:03d} " for i in range(100)) if operation == "stream-long"
                        else "synthetic provider response")
            valid = valid and "".join(value.get("text", "") for _, value in events) == expected
            gaps = [(end[0] - begin[0]) * 1000 for begin, end in zip(events, events[1:])]
            return response.status_code, valid, profile, {"first": first, "gap_ms": gaps}
    response = await client.request(method, paths[operation], headers=headers, **kwargs)
    valid = response.status_code == 200
    extra = {}
    if valid and operation == "download":
        valid = response.content.startswith(f"Owned text fixture {who['index']:04d}\n".encode())
    elif valid and operation == "read":
        values = response.json()
        valid = len(values) == 25 and all(row["text"].startswith(f"Owned synthetic entry {who['index']} ")
                                         for row in values)
    elif valid and operation == "write":
        valid = response.json()["amount"] == iteration + 2
    elif valid and operation == "session":
        valid = response.json()["username"] == f"fixture{who['index']:04d}"
    elif valid and operation == "ai":
        valid = response.json()["text"] == "synthetic provider response"
    elif valid and operation == "external":
        contents = response.json()
        valid = contents.get("result") == 3
        extra["provider_connections"] = contents.get("connections", [])
    return response.status_code, valid, response.headers.get("x-phase0-profile"), extra


async def duplex(socket, actor):
    sent = []
    elapsed = []
    total = 100
    valid = True

    async def send_frames():
        start = monotonic()
        for seq in range(total):
            await asyncio.sleep(max(0, start + seq * 0.02 - monotonic()))
            sent.append(monotonic())
            await socket.send(pack("!HH", actor, seq) * 160)

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(send_frames())
            for seq in range(total):
                frame = await asyncio.wait_for(socket.recv(), 4)
                if not isinstance(frame, bytes) or len(frame) != 640 or unpack_from("!HH", frame) != (actor, seq):
                    valid = False
                    break
                elapsed.append((monotonic() - sent[seq]) * 1000)
    except ExceptionGroup:
        valid = False
    return 101, valid and len(elapsed) == total, None, {
        "rtt_ms": elapsed, "frames_received": len(elapsed), "frames_expected": total,
    }


async def run(args, identities):
    profiles = {op: [] for op in (*MIXES["mixed"], "login", "signup", "echo", "voice", "control",
                                 "persistent", "create-delete", "stream-long", "voice-direct", "voice-duplex")}
    output = gzip.open(args.raw, "wt", encoding="utf-8")
    stats = {"missed": 0, "offered": 0, "loop_ms": []}
    start = monotonic() + 0.25
    stop = start + args.seconds
    inflight = set()
    busy = set()
    mix = MIXES.get(args.workload, {args.workload: 100})

    async with httpx.AsyncClient(base_url=args.url, limits=httpx.Limits(
        max_connections=args.users * 2, max_keepalive_connections=args.users,
    ), trust_env=False, timeout=12) as client:
        def record(sample):
            output.write(json.dumps(sample, separators=(",", ":")) + "\n")
            profiles[sample["op"]].append(sample)

        async def emit(who, operation, iteration, scheduled):
            began = monotonic()
            try:
                code, valid, profile, extra = await request(client, args.url, who, operation, iteration)
                if extra.get("first"):
                    extra["first_ms"] = (extra.pop("first") - scheduled) * 1000
            except Exception as error:
                code, valid, profile, extra = 0, False, None, {"error": type(error).__name__}
            elapsed = monotonic() - scheduled
            sample = {"at": round(scheduled - start, 4), "op": operation, "status": code, "valid": valid,
                      "ms": round(1000 * elapsed, 3), "late_ms": round(1000 * (began - scheduled), 3),
                      **extra}
            if profile:
                sample["profile"] = json.loads(profile)
            record(sample)
            busy.discard(who["index"])

        async def loop_monitor():
            while monotonic() < stop:
                target = monotonic() + 0.05
                await asyncio.sleep(0.05)
                stats["loop_ms"].append(max(0, monotonic() - target) * 1000)

        monitor = asyncio.create_task(loop_monitor())
        rng = random.Random(args.seed)
        seq = 0
        if args.workload == "persistent":
            async def persistent(who):
                await asyncio.sleep(max(0, start - monotonic()) + who["index"] * 0.003)
                opened = monotonic()
                try:
                    async with connect(args.url.replace("http", "ws", 1) + "/ws/realtime/", origin=args.url,
                                       additional_headers={"Cookie": f"sessionid={who['session']}"},
                                       open_timeout=10, close_timeout=1) as socket:
                        connected = monotonic()
                        while monotonic() < stop:
                            payload = {"type": "echo", "text": str(who["index"])}
                            sent = monotonic()
                            await socket.send(json.dumps(payload))
                            answer = json.loads(await asyncio.wait_for(socket.recv(), 3))
                            ms = (monotonic() - sent) * 1000
                            stats["offered"] += 1
                            record({"at": sent - start, "op": "persistent", "status": 101,
                                    "valid": answer == payload, "ms": ms, "rtt_ms": [ms], "late_ms": 0,
                                    "open_ms": (connected - opened) * 1000})
                            await asyncio.sleep(2)
                except Exception as error:
                    stats["offered"] += 1
                    record({"at": opened - start, "op": "persistent", "status": 0, "valid": False,
                            "ms": (monotonic() - opened) * 1000, "late_ms": 0, "error": type(error).__name__})
            await asyncio.gather(*(persistent(who) for who in identities[:args.users]))
        elif args.rate:
            while (target := start + seq / args.rate) < stop:
                remaining = target - monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                who = identities[seq % args.users]
                operation = rng.choices(list(mix), weights=list(mix.values()))[0]
                seq += 1
                stats["offered"] += 1
                if who["index"] in busy or monotonic() - target > 0.25:
                    stats["missed"] += 1
                    continue
                busy.add(who["index"])
                task = asyncio.create_task(emit(who, operation, seq, target))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
        else:
            async def user(who, number):
                randomizer = random.Random(args.seed + number)
                await asyncio.sleep(max(0, start - monotonic()) + number * 0.003)
                iteration = 0
                while monotonic() < stop:
                    operation = randomizer.choices(list(mix), weights=list(mix.values()))[0]
                    stats["offered"] += 1
                    await emit(who, operation, iteration, monotonic())
                    iteration += 1
                    if args.workload not in {"echo", "voice"}:
                        await asyncio.sleep(randomizer.uniform(0.5, 1.5))
            await asyncio.gather(*(user(who, idx) for idx, who in enumerate(identities[:args.users])))
        await asyncio.gather(*inflight)
        await monitor
    output.close()
    groups = {}
    for name, samples in profiles.items():
        if not samples:
            continue
        good = [row for row in samples if row["valid"]]
        p = [row["profile"] for row in samples if "profile" in row]
        statuses = {}
        for row in samples:
            if not row["valid"]:
                key = row.get("error", str(row["status"]))
                statuses[key] = statuses.get(key, 0) + 1
        groups[name] = {
            "count": len(samples), "valid": len(good), "failures": statuses,
            "latency_ms": percentiles([row["ms"] for row in samples]),
            "start_late_ms": percentiles([row["late_ms"] for row in samples]),
            "first_ms": percentiles([row["first_ms"] for row in samples if "first_ms" in row]),
            "gap_ms": percentiles([delay for row in samples for delay in row.get("gap_ms", [])]),
            "rtt_ms": percentiles([delay for row in samples for delay in row.get("rtt_ms", [])]),
            "frames_received": sum(row.get("frames_received", 0) for row in samples),
            "frames_expected": sum(row.get("frames_expected", 0) for row in samples),
            "unique_provider_connections": len({port for row in samples for port in row.get("provider_connections", [])}),
            "provider_calls": sum(len(row.get("provider_connections", ())) for row in samples),
            "profile": {key: percentiles([row[key] for row in p]) for key in
                        ("checkout", "lease", "db", "thread_wait", "thread_run", "bridge", "thread_calls", "lag", "app")} if p else {},
            "queries_p50": median(sum(v[0] for v in row["queries"].values()) for row in p) if p else None,
            "resume_residual_ms": percentiles([max(0, row["bridge"] - row["thread_wait"] - row["thread_run"]) for row in p]),
        }
    complete = sum(item["count"] for item in groups.values())
    return {"users": args.users, "seconds": args.seconds, "rate": args.rate, "mix": args.workload,
            "offered": stats["offered"], "late_or_busy": stats["missed"], "completed": complete,
            "valid": sum(item["valid"] for item in groups.values()), "completed_rps": complete / args.seconds,
            "driver_loop_lag_ms": percentiles(stats["loop_ms"]), "groups": groups}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--identities", type=Path, required=True)
    parser.add_argument("--users", type=int, required=True)
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--rate", type=float)
    parser.add_argument("--seed", type=int, default=40)
    parser.add_argument("--workload", default="mixed")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args, json.loads(args.identities.read_text(encoding="utf-8"))))))


if __name__ == "__main__":
    main()
