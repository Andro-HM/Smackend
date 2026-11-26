"""Run the Phase 4 workload against an installed Smackend distribution."""

import argparse
from contextlib import contextmanager
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import secrets
import subprocess
import sys
from threading import Event, Thread
from time import monotonic, sleep
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql


HERE = Path(__file__).resolve().parent
PHASE0 = HERE.parent / "phase0"


@contextmanager
def process(argv, *, folder, env, log):
    with log.open("w", encoding="utf-8") as file:
        task = subprocess.Popen(argv, cwd=folder, env=env, stdout=file, stderr=subprocess.STDOUT)
        try:
            yield task
        finally:
            if task.poll() is None:
                # Uvicorn's worker supervisor exits on SIGTERM on Linux.
                if os.name == "nt":
                    import psutil

                    parent = psutil.Process(task.pid)
                    children = parent.children(recursive=True)
                    for child in children:
                        child.terminate()
                task.terminate()
                try:
                    task.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    task.kill()
                    task.wait()


def ready(task, url):
    began = monotonic()
    with httpx.Client(trust_env=False, timeout=1) as client:
        while monotonic() - began < 35:
            if task.poll() is not None:
                raise RuntimeError("Server exited during startup. Read its private log.")
            try:
                if client.get(url).status_code == 200:
                    return (monotonic() - began) * 1000
            except httpx.HTTPError:
                pass
            sleep(0.10)
    raise RuntimeError("Server readiness timed out.")


def record_resources(stop, destination, *, app, driver, provider, database, dsn, name):
    import psutil

    start = monotonic()
    with destination.open("w", encoding="utf-8") as output:
        while not stop.is_set():
            groups = {}
            for key, pid in (("app", app), ("driver", driver), ("provider", provider), ("postgres", database)):
                if not pid:
                    continue
                try:
                    root = psutil.Process(pid)
                    values = []
                    for item in [root, *root.children(recursive=True)]:
                        try:
                            with item.oneshot():
                                cpu = item.cpu_times()
                                values.append({"pid": item.pid, "cpu_s": cpu.user + cpu.system,
                                               "rss": item.memory_info().rss, "threads": item.num_threads()})
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                    groups[key] = values
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            try:
                with psycopg.connect(dsn, connect_timeout=2) as connection:
                    connections = connection.execute("""
                        SELECT state, count(*) FROM pg_stat_activity
                        WHERE datname = %s AND backend_type = 'client backend' GROUP BY state
                    """, (name,)).fetchall()
            except psycopg.Error:
                connections = []
            output.write(json.dumps({"elapsed": monotonic() - start, "processes": groups,
                                     "connections": dict(connections)}) + "\n")
            output.flush()
            stop.wait(1)


def checked(args, *, project, env, capture=False):
    return subprocess.run([sys.executable, *args], cwd=project, env=env,
                          check=True, text=True, capture_output=capture)


def check_dataset(dsn, name, project, expected_records, expected_assets):
    with psycopg.connect(dsn, dbname=name) as connection:
        records, wrong_owner = connection.execute("""
            SELECT count(*), count(*) FILTER (
                WHERE r.text NOT LIKE 'Owned synthetic entry ' || substr(u.username, 8)::integer || ' %'
            )
            FROM qualification_record r JOIN accounts_user u ON r.owner_id = u.id
            WHERE r.sequence < 100000
        """).fetchone()
        assets = connection.execute("""
            SELECT blob.key, asset.owner_id = blob.owner_id AND blob.state = 'active'
            FROM media_asset asset JOIN media_blob blob ON asset.blob_id = blob.id
        """).fetchall()
        storage = project / ".smackend-media" / "smackend"
        missing = sum(not (storage / key.hex[:2] / key.hex).is_file() for key, _ in assets)
        inconsistent = sum(not owned for _, owned in assets)
        remaining = connection.execute("SELECT count(*) FROM qualification_record WHERE sequence >= 100000").fetchone()[0]
        socket_leases = connection.execute("SELECT count(*) FROM realtime_socketlease").fetchone()[0]
        owner = connection.execute("SELECT min(owner_id) FROM qualification_record").fetchone()[0]
        plan = connection.execute("""
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT id, sequence, amount, text FROM qualification_record WHERE owner_id = %s ORDER BY sequence LIMIT 25
        """, (owner,)).fetchone()[0][0]
    return {"required_records": expected_records, "owned_records": records, "incorrect_owner_labels": wrong_owner,
            "required_assets": expected_assets, "assets": len(assets), "inconsistent_assets": inconsistent,
            "missing_files": missing, "leftover_create_delete_records": remaining,
            "socket_leases_at_end": socket_leases,
            "quiet_read_execution_ms": plan["Execution Time"], "quiet_read_planning_ms": plan["Planning Time"],
            "correct": records == expected_records and len(assets) == expected_assets
            and not (wrong_owner or inconsistent or missing or remaining)}


def configure(project, env, provider_url):
    app = project / "qualification"
    app.mkdir()
    (app / "__init__.py").touch()
    (app / "models.py").write_bytes((HERE / "workload.py").read_bytes())
    with (project / "config" / "settings.py").open("a", encoding="utf-8") as settings:
        settings.write("""
import os
INSTALLED_APPS += ["qualification"]
QUALIFICATION_EARLY_RELEASE = os.getenv("QUALIFICATION_EARLY_RELEASE", "1") == "1"
if os.getenv("QUALIFICATION_AUTH_IP_BUDGET"):
    SMACKEND_AUTH_LIMITS = {"ip": int(os.environ["QUALIFICATION_AUTH_IP_BUDGET"])}
if os.getenv("QUALIFICATION_PROFILE") == "1":
    from lab.profile_db import base as probe
    probe.TABLE_GROUPS['"qualification_record"'] = "record"
    DATABASES["default"]["ENGINE"] = "lab.profile_db"
""")
    with (project / "config" / "api.py").open("a", encoding="utf-8") as api:
        api.write('\nfrom qualification.models import router\napi.add_router("/bench", router)\n')
    with (project / "config" / "asgi.py").open("a", encoding="utf-8") as app_config:
        app_config.write("""
if os.getenv("QUALIFICATION_PROFILE") == "1":
    from lab.profile_asgi import instrument
    application = instrument(application)
if os.getenv("QUALIFICATION_REUSE") == "1":
    from lifecycle import with_outbound
    application = with_outbound(application)
""")
    env.update(
        PYTHONPATH=os.pathsep.join((str(PHASE0), str(HERE))),
        SMACKEND_HTTP_PROVIDERS=json.dumps({"synthetic": {"base_url": provider_url, "allow_http": True, "attempts": 1}}),
        SMACKEND_AI_MODELS=json.dumps({"synthetic": {"adapter": "openai", "provider": "synthetic",
                                                     "model": "qualification-only", "capabilities": ["text", "stream"]}}),
        SMACKEND_AI_DEMO_MODEL="synthetic",
        SMACKEND_AI_QUOTAS=json.dumps({"app": {"tokens": 10**10, "requests": 200_000, "concurrent": 120},
                                     "user": {"tokens": 10**8, "requests": 10_000, "concurrent": 3}}),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--matrix", nargs="+", required=True, help="Operation, users, seconds and optional arrival rate")
    parser.add_argument("--pool", type=int, default=8)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--port", type=int, default=8084)
    parser.add_argument("--database-pid", type=int)
    parser.add_argument("--psutil", type=Path)
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--rows", type=int, default=100)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--hold-outbound", action="store_true")
    parser.add_argument("--reuse-outbound", action="store_true")
    parser.add_argument("--auth-ip-budget", type=int)
    args = parser.parse_args()
    if args.psutil:
        sys.path.insert(0, str(args.psutil.resolve()))
    import psutil

    dsn = os.environ["SMACKEND_SMOKE_DSN"]
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    loopback = {"127.0.0.1", "localhost", "::1"}
    if params.get("host") not in loopback or params.get("hostaddr", params.get("host")) not in loopback:
        raise ValueError("Use a disposable loopback PostgreSQL cluster.")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("POSTGRES_", "SMACKEND_", "DJANGO_", "PG"))}
    env.update({key: params[field] for key, field in (("POSTGRES_HOST", "host"), ("POSTGRES_PORT", "port"),
                                                     ("POSTGRES_USER", "user"), ("POSTGRES_PASSWORD", "password"))})
    project_home = args.scratch.resolve()
    project_home.mkdir(parents=True, exist_ok=False)
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    name = "smackend_qual_" + uuid4().hex
    url = f"http://127.0.0.1:{args.port}"
    provider_url = f"http://127.0.0.1:{args.port + 1}"
    app_log = project_home / "uvicorn.log"
    # Runtime and cookies stay in the ignored scratch directory.
    env.update(POSTGRES_DB=name, POSTGRES_POOL_MAX_SIZE=str(args.pool), DJANGO_DEBUG="false",
               SMACKEND_LOG_LEVEL="WARNING", SMACKEND_REALTIME_ORIGINS=url,
               SMACKEND_REALTIME_CONNECTION_LIMIT="512", SMACKEND_REALTIME_SECONDS="600",
               QUALIFICATION_EARLY_RELEASE="0" if args.hold_outbound else "1",
               QUALIFICATION_PROFILE="1" if args.profile else "0",
               QUALIFICATION_REUSE="1" if args.reuse_outbound else "0")
    if args.auth_ip_budget:
        env["QUALIFICATION_AUTH_IP_BUDGET"] = str(args.auth_ip_budget)
    started = monotonic()
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        server_version = db.info.server_version
        try:
            checked(["-c", "from smackend.cli import main; raise SystemExit(main())",
                     "init", "qualification-project", "--ai", "--realtime", "--storage", "--admin"],
                    project=project_home, env=env)
            project = project_home / "qualification-project"
            configure(project, env, provider_url)
            checked(["manage.py", "makemigrations", "qualification", "--noinput"], project=project, env=env)
            checked(["manage.py", "migrate", "--noinput", "--verbosity", "0"], project=project, env=env)
            identities = project_home / "private-sessions.json"
            checked(["manage.py", "shell", "-c",
                     f"import json; from pathlib import Path; from qualification.models import seed; "
                     f"Path({str(identities)!r}).write_text(json.dumps(seed({args.users}, {args.rows})), encoding='utf-8')"],
                    project=project, env=env)
            fixture_seconds = monotonic() - started
            with process([sys.executable, "-m", "uvicorn", "provider:application", "--host", "127.0.0.1",
                          "--port", str(args.port + 1), "--no-access-log", "--lifespan", "off"],
                         folder=HERE, env=env, log=project_home / "provider.log") as provider:
                provider_startup = ready(provider, provider_url + "/")
                with process([sys.executable, "-m", "uvicorn", "config.asgi:application", "--host", "127.0.0.1",
                              "--port", str(args.port), "--workers", str(args.workers), "--lifespan",
                              "on" if args.reuse_outbound else "off",
                              "--no-access-log", "--ws-max-size", "4096", "--ws-max-queue", "8"],
                             folder=project, env=env, log=app_log) as app:
                    startup = ready(app, url + "/api/ready")
                    measures = {"release": importlib.metadata.version("smackend"), "platform": platform.platform(),
                                "python": platform.python_version(), "postgres": server_version,
                                "logical_cpus": psutil.cpu_count(), "machine_ram": psutil.virtual_memory().total,
                                "workers": args.workers, "pool_per_worker": args.pool, "users_seeded": args.users,
                                "rows_per_user": args.rows, "app_startup_ms": startup,
                                "provider_startup_ms": provider_startup, "fixture_seconds": fixture_seconds,
                                "profile": args.profile, "hold_outbound": args.hold_outbound}
                    measures["auth_ip_budget_override"] = args.auth_ip_budget
                    measures["reuse_outbound"] = args.reuse_outbound
                    measures["driver_sha256"] = sha256((HERE / "load.py").read_bytes()).hexdigest()
                    evidence.joinpath("environment.json").write_text(json.dumps(measures, indent=2), encoding="utf-8")
                    idle = Event()
                    sample = Thread(target=record_resources, args=(idle, evidence / "idle.jsonl"), kwargs={
                        "app": app.pid, "driver": None, "provider": provider.pid, "database": args.database_pid,
                        "dsn": dsn, "name": name,
                    })
                    sample.start()
                    sleep(5)
                    idle.set()
                    sample.join()
                    for index, scenario in enumerate(args.matrix):
                        operation, user_count, seconds, *rate = scenario.split(":")
                        label = f"{index:02d}-{operation}-{user_count}"
                        driver_args = [sys.executable, str(HERE / "load.py"), "--url",
                                       provider_url if operation in {"control", "voice-direct"} else url,
                                       "--identities", str(identities),
                                       "--users", user_count, "--seconds", seconds, "--raw",
                                       str(evidence / (label + ".jsonl.gz")), "--workload", operation]
                        if rate:
                            driver_args += ["--rate", rate[0]]
                        print(f"Running {scenario}", flush=True)
                        with open(evidence / (label + ".json"), "w", encoding="utf-8") as summary:
                            driver = subprocess.Popen(driver_args, env=env, stdout=summary)
                            stop = Event()
                            sample = Thread(target=record_resources, args=(stop, evidence / (label + "-resources.jsonl")),
                                            kwargs={"app": app.pid, "driver": driver.pid, "provider": provider.pid,
                                                    "database": args.database_pid, "dsn": dsn, "name": name})
                            sample.start()
                            try:
                                status = driver.wait()
                                if status:
                                    raise RuntimeError(f"Load driver exited with status {status}.")
                            finally:
                                stop.set()
                                sample.join()
                        playback = json.loads((evidence / (label + ".json")).read_text(encoding="utf-8"))
                        print(f"{label}: {playback['valid']}/{playback['completed']} valid, "
                              f"{playback['completed_rps']:.1f}/s, {playback['late_or_busy']} missed", flush=True)
                    integrity = check_dataset(dsn, name, project, args.users * args.rows, args.users)
                    (evidence / "integrity.json").write_text(json.dumps(integrity, indent=2), encoding="utf-8")
        finally:
            db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()
