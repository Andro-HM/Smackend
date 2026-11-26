"""Run local ASGI checks and optional measurements against the lab database."""

import argparse
import asyncio
import csv
from collections import Counter
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import psutil

AUTH = "/_allauth/browser/v1/auth/"
ORIGIN = "http://127.0.0.1:8010"
PASSWORD = "Synthetic!purple-bridge-419"
RESULTS = Path("results/local")
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def csrf(client):
    return {"X-CSRFToken": client.cookies["csrftoken"]}


@contextmanager
def server(module, port, workers=1):
    with (RESULTS / f"uvicorn-{port}.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port),
             "--workers", str(workers), "--lifespan", "off", "--no-proxy-headers", "--no-access-log"],
            stdout=log, stderr=subprocess.STDOUT, creationflags=FLAGS,
        )
        try:
            yield child
        finally:
            parent = psutil.Process(child.pid) if child.poll() is None else None
            descendants = parent.children(recursive=True) if parent else []
            if parent:
                child.terminate()
                child.wait(timeout=10)
            for process in descendants:
                if process.is_running():
                    try:
                        process.terminate()
                    except psutil.NoSuchProcess:
                        pass


def wait_for(url, process=None, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process and process.poll() is not None:
            raise RuntimeError(f"Server exited before {url} responded. Check results/local logs.")
        try:
            if httpx.get(url, timeout=1, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {url}")


async def check_provider(client):
    start = time.monotonic()
    responses = await asyncio.gather(*(client.get("/api/provider") for _ in range(10)))
    elapsed = time.monotonic() - start
    assert all(r.status_code == 200 and r.json()["results"] == ["sample-0", "sample-1", "sample-2"] for r in responses), [
        (r.status_code, r.text[:100]) for r in responses
    ]
    slow = asyncio.create_task(client.get("/api/provider?mode=slow"))
    await asyncio.sleep(0.05)
    start = time.monotonic()
    notes = await client.get("/api/notes/")
    notes_elapsed = time.monotonic() - start
    notes_finished_before_timeout = not slow.done()
    assert notes.status_code == 200 and notes_finished_before_timeout
    assert (await slow).status_code == 504
    for mode in ("error", "malformed"):
        assert (await client.get(f"/api/provider?mode={mode}")).status_code == 502
    assert (await client.get("/api/notes/")).status_code == 200
    return {"ten_parallel_seconds": round(elapsed, 3), "notes_during_timeout_seconds": round(notes_elapsed, 3)}


async def smoke():
    async with httpx.AsyncClient(base_url=ORIGIN, timeout=10, trust_env=False) as client:
        assert (await client.get("/api/notes/")).status_code == 401
        assert (await client.get("/api/provider")).status_code == 401
        assert (await client.get("/")).status_code == 200
        new = await client.post(
            AUTH + "signup", headers=csrf(client), json={"username": "live" + uuid4().hex[:16], "password": PASSWORD}
        )
        assert new.status_code == 200, new.text
        assert (await client.get("/notes/")).status_code == 200
        assert (await client.get(AUTH + "session")).status_code == 200
        note = await client.post("/api/notes/", headers=csrf(client), json={"title": "Live smoke"})
        assert note.status_code == 201, note.text
        data = await check_provider(client)
        assert (await client.delete(f"/api/notes/{note.json()['id']}", headers=csrf(client))).status_code == 204
        assert (await client.delete(AUTH + "session", headers=csrf(client))).status_code == 401
        assert (await client.get("/api/notes/")).status_code == 401
        assert (await client.get("/api/provider")).status_code == 401
        return data


def sample_process_tree(pid):
    try:
        parent = psutil.Process(pid)
        with parent.oneshot():
            processes = [parent, *parent.children(recursive=True)]
    except psutil.NoSuchProcess:
        return {}
    measurements = {}
    for process in processes:
        try:
            with process.oneshot():
                cpu = process.cpu_times()
                measurements[process.pid] = (process.memory_info().rss, cpu.user + cpu.system)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return measurements


def load_test(app, users, seconds, db_pid):
    target = RESULTS / f"users-{users}"
    log_path = target.with_suffix(".log")
    with log_path.open("w", encoding="utf-8") as log:
        runner = subprocess.Popen(
            [sys.executable, "-m", "locust", "--headless", "--only-summary", "--host", ORIGIN,
             "-u", str(users), "-r", "5", "-t", f"{seconds + (users + 4) // 5}s", "--csv", str(target),
             "--exit-code-on-error", "1"],
            stdout=log, stderr=subprocess.STDOUT, creationflags=FLAGS,
        )
    start = time.monotonic()
    peaks = Counter()
    cpu_first = {}
    cpu_last = {}
    while runner.poll() is None:
        for label, pid in (("app", app.pid), ("postgres", db_pid)):
            if pid is None:
                continue
            samples = sample_process_tree(pid)
            peaks[label] = max(peaks[label], sum(rss for rss, _ in samples.values()))
            for process_id, (_, cpu) in samples.items():
                cpu_first.setdefault((label, process_id), cpu)
                cpu_last[label, process_id] = cpu
        time.sleep(0.5)
    elapsed = time.monotonic() - start
    if runner.returncode:
        print(f"Locust exited {runner.returncode} at {users} users", flush=True)
    stats_path = Path(f"{target}_stats.csv")
    rows = []
    if stats_path.exists():
        with stats_path.open(newline="", encoding="utf-8") as report:
            rows = list(csv.DictReader(report))
    print(log_path.read_text(encoding="utf-8", errors="replace")[-8500:], flush=True)
    return {
        "users": users,
        "exit_code": runner.returncode,
        "peak_sampled_rss_mib": {label: round(rss / 2**20, 1) for label, rss in peaks.items()},
        "sampled_cpu_seconds": {
            label: round(sum(cpu_last[k] - cpu_first[k] for k in cpu_first if k[0] == label), 2) for label in peaks
        },
        "wall_seconds": round(elapsed, 2),
        "stats": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--load", action="store_true")
    parser.add_argument("--seconds", type=int, default=20, help="Seconds after the planned ramp at each level")
    parser.add_argument("--db-pid", type=int, help="Native PostgreSQL postmaster PID, for resource sampling")
    args = parser.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    report = {"host": platform.platform(), "logical_cpus": os.cpu_count(), "workers": 2}
    with ExitStack() as stack:
        stub = stack.enter_context(server("provider_stub:application", 8011))
        app = stack.enter_context(server("lab.asgi:application", 8010, workers=2))
        wait_for("http://127.0.0.1:8011/sample", stub)
        wait_for(ORIGIN + "/ready", app)
        report["smoke"] = asyncio.run(smoke())
        print(json.dumps(report["smoke"]), flush=True)
        if args.load:
            report["load"] = []
            for users in (10, 20, 50, 100):
                report["load"].append(load_test(app, users, args.seconds, args.db_pid))
                (RESULTS / "measurements.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (RESULTS / "measurements.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if any(row["exit_code"] for row in report.get("load", [])):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
