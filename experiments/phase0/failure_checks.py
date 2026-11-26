"""Measure loopback rate-limit behavior and restart only the named portable lab cluster."""

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import httpx
from dotenv import load_dotenv
import psycopg
from django.utils.crypto import salted_hmac

from live_checks import AUTH, ORIGIN, PASSWORD, RESULTS, csrf, server, wait_for


def database(**kwargs):
    return psycopg.connect(
        host="127.0.0.1", port=os.environ["POSTGRES_PORT"], dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"], connect_timeout=2, **kwargs
    )


async def rate_burst(username, count):
    async with httpx.AsyncClient(
        base_url=ORIGIN, timeout=15, trust_env=False,
        limits=httpx.Limits(max_connections=count, max_keepalive_connections=0),
    ) as client:
        assert (await client.get("/")).status_code == 200
        start = time.monotonic()
        replies = await asyncio.gather(*(
            client.post((ORIGIN if index % 2 else "http://127.0.0.1:8012") + AUTH + "login", headers={
                **csrf(client), "Host": "127.0.0.1:8010",
            }, json={
                "username": username.upper() if index % 2 else username,
                "password": "Deliberately incorrect synthetic password",
            })
            for index in range(count)
        ))
        expected_user = "unrelated" + uuid4().hex[:12]
        assert (await client.post(AUTH + "signup", headers=csrf(client), json={
            "username": expected_user, "password": PASSWORD,
        })).status_code == 200
        assert (await client.delete(AUTH + "session", headers=csrf(client))).status_code == 401
        control = await client.post(AUTH + "login", headers=csrf(client), json={
            "username": expected_user, "password": PASSWORD,
        })
        return {
            "burst_and_control_seconds": round(time.monotonic() - start, 3),
            "statuses": dict(Counter(r.status_code for r in replies)),
            "workers": dict(Counter(r.headers.get("X-Lab-Worker", "unknown") for r in replies)),
            "codes": dict(Counter(
                err.get("code", "unknown")
                for r in replies if r.status_code == 400 for err in r.json().get("errors", [])
            )),
            "replies_with_retry_after": sum("Retry-After" in r.headers for r in replies),
            "unrelated_login_status": control.status_code,
        }


def matched_rates():
    report = {}
    for name, strict, count in (("allauth_cache", "0", 100), ("atomic_counter", "1", 100)):
        os.environ["PHASE0_STRICT_RATE"] = strict
        os.environ["PHASE0_RATE_PROBE_ATOMIC_ONLY"] = strict
        username = "quota" + uuid4().hex[:16]
        # Start far enough from a bucket boundary that 100 attempts land in one window.
        if strict == "1":
            with database() as conn:
                position = conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()))::bigint % 60").fetchone()[0]
            if position > 43:
                time.sleep(61 - position)
        with ExitStack() as sources:
            for port in (8010, 8012):
                app = sources.enter_context(server("lab.asgi:application", port))
                wait_for(f"http://127.0.0.1:{port}/ready", app)
            row = asyncio.run(rate_burst(username, count))
        if strict == "1":
            key = salted_hmac("phase0.login", username, secret=os.environ["DJANGO_SECRET_KEY"], algorithm="sha256").hexdigest()
            with database() as conn:
                buckets = conn.execute("SELECT attempts FROM phase0_login_budget WHERE key = %s", [key]).fetchall()
            row["stored_bucket_counts"] = [bucket[0] for bucket in buckets]
            row["pass"] = (
                row["stored_bucket_counts"] == [10]
                and row["statuses"] == {400: 10, 429: 90}
                and row["replies_with_retry_after"] == 90
                and len(row["workers"]) == 2
                and row["unrelated_login_status"] == 200
            )
        report[name] = row
        print(f"{name}: {json.dumps(row)}", flush=True)
    os.environ.pop("PHASE0_RATE_PROBE_ATOMIC_ONLY", None)
    return report


def pg_ctl(action):
    command = [str(Path(".local/postgres/bin/pg_ctl.exe").resolve()), "-D", ".local/pgdata", "-w"]
    if action == "start":
        command += ["-l", ".local/postgres.log", "-o", f"-h 127.0.0.1 -p {int(os.environ['POSTGRES_PORT'])} -c max_connections=50"]
    else:
        command += ["-m", "fast"]
    subprocess.run([*command, action], check=True)


def restart_probe():
    with server("lab.asgi:application", 8010, workers=2) as app, httpx.Client(
        base_url=ORIGIN, timeout=12, trust_env=False
    ) as client:
        wait_for(ORIGIN + "/ready", app)
        assert client.get("/").status_code == 200
        assert client.post(AUTH + "signup", headers=csrf(client), json={
            "username": "recovery" + uuid4().hex[:12], "password": PASSWORD,
        }).status_code == 200
        saved = client.post("/api/notes/", headers=csrf(client), json={"title": "Survives restart"})
        assert saved.status_code == 201
        path = f"/api/notes/{saved.json()['id']}"
        pg_ctl("stop")
        try:
            start = time.monotonic()
            ready = client.get("/ready")
            read = client.get(path)
            live = client.get("/live")
            outage = {"ready": ready.status_code, "notes": read.status_code, "live": live.status_code,
                      "outage_probe_seconds": round(time.monotonic() - start, 3)}
        finally:
            start = time.monotonic()
            pg_ctl("start")
        wait_for(ORIGIN + "/ready", app)
        deadline = time.monotonic() + 25
        response = ready
        while time.monotonic() < deadline:
            response = client.get(path)
            if response.status_code == 200:
                break
            time.sleep(0.2)
        outage["recovery_seconds_from_start_command"] = round(time.monotonic() - start, 3)
        outage["saved_note_survived"] = response.status_code == 200 and response.json() == saved.json()
        outage["pass"] = (
            (outage["ready"], outage["notes"], outage["live"]) == (503, 503, 200)
            and outage["saved_note_survived"]
        )
        return outage


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart-portable", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    RESULTS.mkdir(parents=True, exist_ok=True)
    report = matched_rates()
    if args.restart_portable:
        report["restart"] = restart_probe()
    output = json.dumps(report, indent=2)
    (RESULTS / "failure-checks.json").write_text(output, encoding="utf-8")
    print(output, flush=True)
    if not report["atomic_counter"]["pass"] or (args.restart_portable and not report["restart"]["pass"]):
        raise SystemExit(1)
