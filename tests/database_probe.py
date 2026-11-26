"""Runs only with the dependencies of a freshly generated project."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Barrier, Event
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg
from psycopg import sql

FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

WORKER = """
import asyncio
from pathlib import Path

from asgiref.sync import sync_to_async
from config.asgi import application
from config.api import api
from django.db import OperationalError, connection, transaction


def value(query):
    with connection.cursor() as cursor:
        cursor.execute(query)
        return cursor.fetchone()[0]


@api.get("/_test/pid")
def pid(request):
    return {"backend": value("SELECT pg_backend_pid()")}


@api.get("/_test/hold")
def hold(request):
    return {"backend": value("SELECT pg_backend_pid(), pg_sleep(3)")}


@api.get("/_test/cancel")
def cancel(request):
    with transaction.atomic():
        value("SELECT set_config('statement_timeout', '100ms', true)")
        value("SELECT pg_sleep(1)")


@api.get("/_test/value")
def saved(request):
    return {"value": value("SELECT value FROM phase13_counter WHERE id = 1")}


@api.post("/_test/committed_error")
def committed_error(request):
    value("UPDATE phase13_counter SET value = value + 1 WHERE id = 1 RETURNING value")
    raise OperationalError("private diagnostic after a committed test write")


@sync_to_async
def read_before_wait():
    try:
        return value("SELECT pg_backend_pid()")
    finally:
        connection.close()


@api.get("/_test/external_wait")
async def external_wait(request):
    backend = await read_before_wait()
    Path("external-started").touch()
    await asyncio.sleep(2)
    return {"backend": backend}
"""


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_until(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.04)
    raise AssertionError("Timed out waiting for the PostgreSQL probe")


def request(origin, route, timeout=6, *, method="GET"):
    try:
        response = urlopen(Request(origin + "/api/" + route, method=method), timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response), response.headers.get("Cache-Control")


def assert_failure(response, expected_status, code, *, readiness=False):
    status, body, cache = response
    assert status == expected_status and cache == "no-store"
    assert body["code"] == code and body["message"]
    assert "private diagnostic" not in str(body)
    if readiness:
        assert body["status"] == "unavailable"


@contextmanager
def worker(name):
    (ROOT / "probe_worker.py").write_text(WORKER, encoding="utf-8")
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    command = [
        sys.executable, "-m", "uvicorn", "probe_worker:application", "--host", "127.0.0.1",
        "--port", str(port), "--lifespan", "off", "--no-proxy-headers",
    ]
    with (ROOT / f"{name}.log").open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, creationflags=FLAGS,
            env={**os.environ, "PGAPPNAME": name},
        )

        def online():
            if process.poll() is not None:
                raise AssertionError("Uvicorn exited during startup")
            try:
                return request(origin, "health", timeout=1) == (200, {"status": "ok"}, "no-store")
            except OSError:
                return False

        try:
            wait_until(online)
            yield origin, process
        except Exception:
            log.seek(0)
            print(log.read())
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


@contextmanager
def disposable_database():
    dsn = os.environ["SMACKEND_SMOKE_DSN"]
    credentials = psycopg.conninfo.conninfo_to_dict(dsn)
    local = {"127.0.0.1", "localhost", "::1"}
    assert credentials.get("host") in local and credentials.get("hostaddr", credentials["host"]) in local
    assert credentials.get("password"), "Supply a password in the test DSN"
    name = "smackend_phase13_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        os.environ.update(
            POSTGRES_DB=name, POSTGRES_USER=admin.info.user, POSTGRES_PASSWORD=credentials["password"],
            POSTGRES_HOST=admin.info.host, POSTGRES_PORT=str(admin.info.port),
        )
        try:
            yield admin, name
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def transactions():
    import django
    from django.db import DatabaseError, connection, transaction

    def query(statement, params=None):
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
            return cursor.fetchone()[0] if cursor.description else None

    with disposable_database():
        os.environ.update(
            POSTGRES_POOL_MAX_SIZE="2", POSTGRES_STATEMENT_TIMEOUT_MS="1200",
            POSTGRES_LOCK_TIMEOUT_MS="200", POSTGRES_IDLE_IN_TRANSACTION_TIMEOUT_MS="6000",
        )
        django.setup()
        try:
            expected = {"statement_timeout": 1200, "lock_timeout": 200, "idle_in_transaction_session_timeout": 6000}
            for name, ms in expected.items():
                assert query("SELECT setting::int FROM pg_settings WHERE name = %s", [name]) == ms
            assert query("SHOW transaction_isolation") == "read committed"
            query("CREATE TABLE phase13_counter (id int PRIMARY KEY, value int NOT NULL)")
            query("INSERT INTO phase13_counter VALUES (1, 0)")
            try:
                with transaction.atomic():
                    query("UPDATE phase13_counter SET value = 99 WHERE id = 1")
                    raise ValueError("application asked to roll back")
            except ValueError:
                assert query("SELECT value FROM phase13_counter") == 0
            connection.close()

            updated, commit = Event(), Event()

            def change():
                try:
                    with transaction.atomic():
                        query("UPDATE phase13_counter SET value = 10 WHERE id = 1")
                        updated.set()
                        assert commit.wait(timeout=4)
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as threads:
                operation = threads.submit(change)
                try:
                    assert updated.wait(timeout=5)
                    assert query("SELECT value FROM phase13_counter") == 0
                    try:
                        with transaction.atomic():
                            query("UPDATE phase13_counter SET value = 77 WHERE id = 1")
                    except DatabaseError as error:
                        assert error.__cause__.sqlstate == "55P03"
                    else:
                        raise AssertionError("Contended update did not hit lock_timeout")
                finally:
                    connection.close()
                    commit.set()
                operation.result(timeout=5)
                assert query("SELECT value FROM phase13_counter") == 10
                connection.close()

                gate = Barrier(2, timeout=5)

                def increment():
                    try:
                        with transaction.atomic():
                            gate.wait()
                            query("UPDATE phase13_counter SET value = value + 1 WHERE id = 1")
                    finally:
                        connection.close()

                first, second = (threads.submit(increment) for _ in range(2))
                first.result(timeout=5)
                second.result(timeout=5)
                assert query("SELECT value FROM phase13_counter") == 12

            try:
                with transaction.atomic():
                    query("SELECT pg_sleep(2)")
            except DatabaseError as error:
                assert error.__cause__.sqlstate == "57014"
            else:
                raise AssertionError("Statement timeout did not cancel pg_sleep")

            old_backend = query("SELECT pg_backend_pid()")
            try:
                with transaction.atomic():
                    query("SELECT set_config('idle_in_transaction_session_timeout', '250ms', true)")
                    time.sleep(0.75)
                    query("SELECT 1")
            except DatabaseError:
                connection.close()
            else:
                raise AssertionError("Idle transaction did not lose its connection")
            assert query("SELECT pg_backend_pid()") != old_backend
            assert query("SELECT value FROM phase13_counter") == 12
            print("Observed commit, rollback, read committed isolation, two retained increments and all three SQL timeouts.")
        finally:
            connection.close()
            # This process owns the test pool. Close idle members before dropping the DB.
            connection.close_pool()


def workers():
    with disposable_database() as (admin, name):
        with psycopg.connect(os.environ["SMACKEND_SMOKE_DSN"], dbname=name, connect_timeout=3) as setup:
            setup.execute("CREATE TABLE phase13_counter (id int PRIMARY KEY, value int NOT NULL)")
            setup.execute("INSERT INTO phase13_counter VALUES (1, 0)")
        os.environ.update(
            POSTGRES_POOL_MIN_SIZE="1", POSTGRES_POOL_MAX_SIZE="1", POSTGRES_POOL_TIMEOUT_SECONDS="1",
            POSTGRES_STATEMENT_TIMEOUT_MS="4500",
        )

        def activity():
            return admin.execute(
                "SELECT application_name, pid, wait_event FROM pg_stat_activity WHERE datname = %s",
                [name],
            ).fetchall()

        with worker("smackend-a") as (a, process_a), worker("smackend-b") as (b, process_b):
            assert process_a.pid != process_b.pid
            pid_a = request(a, "_test/pid")[1]["backend"]
            pid_b = request(b, "_test/pid")[1]["backend"]
            assert pid_a != pid_b

            with ThreadPoolExecutor(max_workers=5) as threads:
                held_a = threads.submit(request, a, "_test/hold")
                held_b = threads.submit(request, b, "_test/hold")
                wait_until(lambda: {pid for _, pid, event in activity() if event == "PgSleep"} == {pid_a, pid_b})
                started = time.monotonic()
                jobs = [
                    threads.submit(request, a, "ready"),
                    threads.submit(request, b, "ready"),
                    threads.submit(request, a, "_test/pid"),
                ]
                for job in jobs[:2]:
                    assert_failure(job.result(), 503, "database_unavailable", readiness=True)
                assert_failure(jobs[2].result(), 503, "database_busy")
                elapsed = time.monotonic() - started
                assert 0.8 <= elapsed < 2.8, elapsed
                assert len(activity()) == 2
                assert request(a, "health") == (200, {"status": "ok"}, "no-store")
                assert held_a.result()[1]["backend"] == pid_a
                assert held_b.result()[1]["backend"] == pid_b

                assert_failure(request(a, "_test/cancel"), 504, "database_query_cancelled")
                pending = threads.submit(request, a, "_test/external_wait")
                wait_until(lambda: (ROOT / "external-started").is_file())
                assert request(a, "ready") == (200, {"status": "ready"}, "no-store")
                assert not pending.done(), "Readiness finished only after the outbound wait"
                assert pending.result()[:2] == (200, {"backend": pid_a})

            for _ in range(5):
                assert request(a, "_test/pid")[1]["backend"] == pid_a
                assert request(b, "_test/pid")[1]["backend"] == pid_b
            assert {pid for _, pid, _ in activity()} == {pid_a, pid_b}
            assert_failure(request(a, "_test/committed_error", method="POST"), 503, "database_unavailable")
            assert request(b, "_test/value")[:2] == (200, {"value": 1})
        wait_until(lambda: not activity())
        print(f"Two worker processes used two backends, pool exhaustion returned 503 in {elapsed:.3f}s, and both pools cleaned up.")


def restart():
    from dotenv import dotenv_values

    bin_dir = Path(os.environ["SMACKEND_POSTGRES_BIN"]).resolve()
    suffix = ".exe" if os.name == "nt" else ""
    data = ROOT / "restart-pgdata"
    port = free_port()
    password = dotenv_values(ROOT / ".env")["POSTGRES_PASSWORD"]
    password_file = ROOT / "restart-password"
    with os.fdopen(os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        output.write(password + "\n")
    try:
        subprocess.run(
            [str(bin_dir / f"initdb{suffix}"), "-D", str(data), "-U", "smackend", f"--pwfile={password_file}",
             "--auth=scram-sha-256", "--encoding=UTF8", "--locale=C", "--no-sync"],
            check=True, timeout=35,
        )
    finally:
        password_file.unlink()

    def postgres(action):
        options = ["-l", str(ROOT / "postgres.log"), "-o", f"-h 127.0.0.1 -p {port}"] if action == "start" else ["-m", "fast"]
        subprocess.run([str(bin_dir / f"pg_ctl{suffix}"), "-D", str(data), "-w", *options, action],
                       check=True, timeout=20)

    os.environ.update(
        POSTGRES_DB="postgres", POSTGRES_HOST="127.0.0.1", POSTGRES_PORT=str(port), POSTGRES_USER="smackend",
        POSTGRES_PASSWORD=password, POSTGRES_POOL_MAX_SIZE="1", POSTGRES_POOL_TIMEOUT_SECONDS="1",
    )
    postgres("start")
    running = True
    try:
        with psycopg.connect(host="127.0.0.1", port=port, dbname="postgres", user="smackend",
                             password=password, connect_timeout=3) as db:
            db.execute("CREATE TABLE phase13_counter (id int PRIMARY KEY, value int NOT NULL)")
            db.execute("INSERT INTO phase13_counter VALUES (1, 42)")
        with worker("smackend-restart") as (origin, process):
            assert request(origin, "_test/value")[:2] == (200, {"value": 42})
            postgres("stop")
            running = False
            assert request(origin, "health") == (200, {"status": "ok"}, "no-store")
            assert_failure(request(origin, "ready"), 503, "database_unavailable", readiness=True)
            assert request(origin, "_test/value")[0] == 503
            started = time.monotonic()
            postgres("start")
            running = True
            wait_until(lambda: request(origin, "ready") == (200, {"status": "ready"}, "no-store"), timeout=20)
            assert process.poll() is None
            assert request(origin, "_test/value")[:2] == (200, {"value": 42})
            print(f"The same Uvicorn process recovered the saved value {time.monotonic() - started:.3f}s after the restart command.")
    finally:
        if running:
            postgres("stop")


if __name__ == "__main__":
    {"transactions": transactions, "workers": workers, "restart": restart}[sys.argv[1]]()
