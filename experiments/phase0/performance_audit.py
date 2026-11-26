"""Measure one or more isolated, loopback Phase 0 workload variants."""

import argparse
from collections import Counter
from contextlib import ExitStack
import json
from math import ceil
import os
from pathlib import Path
import re
import subprocess
import sys
from time import monotonic, sleep

from dotenv import load_dotenv
import psutil

from failure_checks import database
from live_checks import FLAGS, ORIGIN, RESULTS, sample_process_tree, server, wait_for


def percentile(values, q):
    if not values:
        return None
    return round(sorted(values)[ceil(q * len(values)) - 1], 2)


def summarize(rows):
    data = {}
    for route in sorted({row[1] for row in rows}):
        selected = [r for r in rows if r[1] == route]
        timings = [r[4] for r in selected if r[4]]
        tail = [r[4] for r in selected if r[4] and r[2] >= percentile([r[2] for r in selected], 0.95)]
        keys = ("app", "db", "checkout", "lease", "thread_wait", "thread_run", "lag", "provider", "bridge", "sync_cpu")
        data[route] = {
            "count": len(selected),
            "errors": sum(not r[3] for r in selected),
            "client_p95_ms": percentile([r[2] for r in selected], 0.95),
            "client_p99_ms": percentile([r[2] for r in selected], 0.99),
            "scheduled_action_delay_p95_ms": percentile([r[5] for r in selected if len(r) > 5], 0.95),
            "outside_app_p95_ms": percentile([max(0, r[2] - r[4]["app"]) for r in selected if r[4]], 0.95),
            "timing_p95_ms": {
                key: percentile([t[key] for t in timings if key in t], 0.95) for key in keys
            },
            "slowest_five_percent_median_ms": {
                key: percentile([t[key] for t in tail if key in t], 0.5) for key in keys
            },
            "thread_calls_p50": percentile([t["thread_calls"] for t in timings], 0.5),
            "bridge_outside_executor_p95_ms": percentile(
                [max(0, t["bridge"] - t["thread_run"] - t["thread_wait"]) for t in timings if "bridge" in t], 0.95
            ),
            "query_groups": {
                group: {
                    "calls_per_request": round(sum(t["queries"].get(group, [0, 0])[0] for t in timings) / len(timings), 2),
                    "p95_ms_per_request": percentile([t["queries"].get(group, [0, 0])[1] for t in timings], 0.95),
                }
                for group in sorted({g for t in timings for g in t["queries"]})
            },
        }
    return data


def sample_resources(roots, resource):
    for name, pid in roots.items():
        points = sample_process_tree(pid)
        row = resource.setdefault(name, {"rss": 0, "threads": 0, "cpu": {}})
        row["rss"] = max(row["rss"], sum(point[0] for point in points.values()))
        threads = 0
        for process_id, (_, cpu) in points.items():
            span = row["cpu"].setdefault(process_id, [cpu, cpu])
            span[1] = cpu
            try:
                threads += psutil.Process(process_id).num_threads()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        row["threads"] = max(row["threads"], threads)


def collect(run, seconds, users, app, stub, db_pid, driver, crud_only, offered_actions):
    sample = RESULTS / f"audit-{run}-samples.json"
    log = RESULTS / f"audit-{run}-driver.log"
    environment = {**os.environ, "PHASE0_AUDIT_OUTPUT": str(sample), "PHASE0_AUDIT_CRUD_ONLY": str(int(crud_only))}
    if driver == "httpx":
        command = [sys.executable, "profile_load.py", str(seconds), str(users), str(sample), str(offered_actions)]
    else:
        command = [sys.executable, "-m", "locust", "--headless", "--only-summary", "--host", ORIGIN,
                   "-u", str(users), "-r", "5", "-t", f"{seconds}s"]
    if sample.exists():
        sample.unlink()
    resource = {}
    counts = Counter()
    with log.open("w", encoding="utf-8") as output, database(autocommit=True) as conn:
        process = subprocess.Popen(
            command, stdout=output, stderr=subprocess.STDOUT, env=environment, creationflags=FLAGS,
        )
        start = monotonic()
        roots = {"app": app.pid, "postgres": db_pid, "driver": process.pid, "simulator": stub.pid}
        while process.poll() is None:
            sample_resources(roots, resource)
            states = conn.execute(
                """SELECT state, wait_event_type, count(*) FROM pg_stat_activity
                   WHERE datname = current_database() AND pid <> pg_backend_pid()
                   GROUP BY state, wait_event_type"""
            ).fetchall()
            counts["sessions"] = max(counts["sessions"], sum(n for _, _, n in states))
            for state, wait, n in states:
                key = f"{state}/{wait}"
                counts[key] = max(counts[key], n)
            sleep(0.45)
        wall = monotonic() - start
    if not sample.exists():
        raise RuntimeError(f"Driver exited {process.returncode} without writing samples. See {log}.")
    rows = json.loads(sample.read_text(encoding="utf-8"))
    steady_start = users / 5 + 3
    steady = [r for r in rows if r[0] >= steady_start and r[1] not in {"bootstrap", "signup"}]
    if not steady:
        raise RuntimeError(f"No steady-window samples were recorded. See {log}.")
    end = max(r[0] for r in steady)
    report = {
        "driver": driver, "crud_only": crud_only, "users": users, "wall_seconds": round(wall, 2),
        "offered_actions_per_second": offered_actions,
        "pool_per_worker": int(os.environ["PHASE0_PROFILE_POOL_SIZE"]),
        "provider_auth_mode": os.getenv("PHASE0_PROVIDER_RELEASE_DB", "0"),
        "inline_headers": os.getenv("PHASE0_INLINE_HEADERS") == "1",
        "exit_code": process.returncode,
        "steady_rps": round(sum(r[1] != "missed_action" for r in steady) / (end - steady_start), 2),
        "steady_seconds": round(end - steady_start, 2),
        "worker_responses": dict(Counter(r[4]["pid"] for r in steady if r[4])),
        "resources": {
            name: {
                "peak_rss_mib": round(row["rss"] / 2**20, 1), "peak_threads": row["threads"],
                "per_pid_cpu_seconds": {pid: round(end - start, 2) for pid, (start, end) in row["cpu"].items()},
            }
            for name, row in resource.items()
        },
        "postgres_sample_peaks": counts,
        "overall": summarize(rows),
        "steady": summarize(steady),
    }
    (RESULTS / f"audit-{run}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(run, report["steady_rps"], report["worker_responses"], flush=True)


def query_plans():
    def measure(conn, query, params=()):
        plan = conn.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}", params).fetchone()[0][0]

        def shape(node):
            result = {key: node[key] for key in ("Node Type", "Index Name", "Actual Rows", "Actual Total Time",
                                                 "Shared Hit Blocks", "Shared Read Blocks") if key in node}
            if "Plans" in node:
                result["children"] = [shape(child) for child in node["Plans"]]
            return result

        return {"execution_ms": plan["Execution Time"], "plan": shape(plan["Plan"])}

    with database() as conn:
        uid = conn.execute("SELECT id FROM accounts_user ORDER BY id DESC LIMIT 1").fetchone()[0]
        key = conn.execute("SELECT session_key FROM django_session ORDER BY expire_date DESC LIMIT 1").fetchone()[0]
        results = {
            "notes_rows": conn.execute("SELECT count(*) FROM notes_note").fetchone()[0],
            "users_rows": conn.execute("SELECT count(*) FROM accounts_user").fetchone()[0],
            "session_rows": conn.execute("SELECT count(*) FROM django_session").fetchone()[0],
            "session": measure(conn, "SELECT * FROM django_session WHERE session_key = %s AND expire_date > now()", (key,)),
            "user": measure(conn, "SELECT * FROM accounts_user WHERE id = %s LIMIT 21", (uid,)),
            "notes_list": measure(conn, "SELECT * FROM notes_note WHERE owner_id = %s ORDER BY id DESC LIMIT 100", (uid,)),
        }
        conn.execute("CREATE TEMP TABLE audit_notes (LIKE notes_note INCLUDING INDEXES) ON COMMIT DROP")
        conn.execute(
            """INSERT INTO audit_notes (id, owner_id, title, body)
               SELECT i, (i % 1000) + 1, 'Synthetic', 'Test note' FROM generate_series(1, 100000) AS i"""
        )
        conn.execute("ANALYZE audit_notes")
        results["notes_list_100k"] = measure(
            conn, "SELECT * FROM audit_notes WHERE owner_id = %s ORDER BY id DESC LIMIT 100", (1,)
        )
    (RESULTS / "audit-query-plans.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label")
    parser.add_argument("--db-pid", type=int)
    parser.add_argument("--plans-only", action="store_true")
    parser.add_argument("--driver", choices=["locust", "httpx"], default="locust")
    parser.add_argument("--offered-actions", type=int, default=0, help="Pace HTTPX user actions per second after signup")
    parser.add_argument("--seconds", type=int, default=40)
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--crud-only", action="store_true")
    parser.add_argument("--pool-size", type=int, choices=[8, 12], default=8)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", args.label):
        parser.error("Use a label of 1–60 letters, digits, underscores, or hyphens")
    if args.users < 1 or args.seconds <= args.users / 5 + 3:
        parser.error("Allow time for the signup ramp and a steady window")
    if args.offered_actions < 0 or (args.offered_actions and (args.driver != "httpx" or args.crud_only)):
        parser.error("--offered-actions requires the mixed HTTPX workload")
    load_dotenv()
    os.environ["PHASE0_PROFILE"] = "1"
    os.environ["PHASE0_PROFILE_POOL_SIZE"] = str(args.pool_size)
    RESULTS.mkdir(parents=True, exist_ok=True)
    if args.plans_only:
        query_plans()
    else:
        if args.db_pid is None:
            parser.error("--db-pid is required for load profiling")
        with ExitStack() as stack:
            stub = stack.enter_context(server("provider_stub:application", 8011))
            app = stack.enter_context(server("lab.asgi:application", 8010, workers=2))
            wait_for("http://127.0.0.1:8011/sample", stub)
            wait_for(ORIGIN + "/ready", app)
            collect(args.label, args.seconds, args.users, app, stub, args.db_pid, args.driver, args.crud_only,
                    args.offered_actions)
