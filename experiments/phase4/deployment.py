"""Isolated Linux HTTPS, database TLS, restore and outage qualification."""

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import ssl
import subprocess
import sys
from threading import Event, Thread
from time import monotonic, sleep

import httpx
import psutil
import psycopg
from psycopg import sql
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from rehearsal import database, run
from run import process


PASSWORD = "Fixture-only!Smackend-phase-four-275"
TLS_ORIGIN = "https://127.0.0.1:8445"
PROXY_HOST = "qualification.example.test"
BROWSER_ORIGIN = f"https://{PROXY_HOST}"
PORT = 8088
NO_FORWARD = {"connection", "transfer-encoding", "proxy-connection", "keep-alive",
              "te", "trailer", "upgrade", "proxy-authenticate", "proxy-authorization", "expect"}


@contextmanager
def proxy(cert, key):
    # A qualification-only loopback proxy. Production deployments should use the
    # reviewed ingress configuration in PHASE_4.md.
    class Handler(BaseHTTPRequestHandler):
        def relay(self):
            headers = {key: value for key, value in self.headers.items()
                       if key.lower() not in NO_FORWARD and not key.lower().startswith("x-forwarded-")}
            headers["X-Forwarded-Proto"] = "https"
            headers["X-Forwarded-For"] = self.client_address[0]
            upstream = HTTPConnection("127.0.0.1", PORT, timeout=8)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    upstream.request(self.command, self.path, self.rfile.read(length), headers)
                    response = upstream.getresponse()
                except (ConnectionError, TimeoutError):
                    self.send_error(503, "Qualification backend temporarily unavailable.")
                    return
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in NO_FORWARD:
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(response.read())
            finally:
                upstream.close()

        do_GET = do_POST = relay

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 8445), Handler) as server:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield ssl.create_default_context(cafile=cert)
        finally:
            server.shutdown()
            thread.join()


def configure_postgresql_tls(cert, key):
    dsn = os.environ["SMACKEND_SMOKE_DSN"]
    local = Path(os.environ["SMACKEND_QUAL_PG_DATA"]).resolve()
    if local.name != "db" or not local.parent.name.startswith("smackend-phase4."):
        raise ValueError("Run this probe through the disposable Phase 4 Linux launcher.")
    with psycopg.connect(dsn, autocommit=True) as admin:
        if Path(admin.execute("SHOW data_directory").fetchone()[0]).resolve() != local:
            raise ValueError("The DSN does not address the disposable cluster.")
        for setting, value in (("ssl_cert_file", str(cert)), ("ssl_key_file", str(key)), ("ssl", "on")):
            admin.execute(sql.SQL("ALTER SYSTEM SET {} TO {}").format(sql.Identifier(setting), sql.Literal(value)))
    cluster("restart")
    with psycopg.connect(dsn, sslmode="verify-full", sslrootcert=str(cert)) as secured:
        assert secured.execute("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()").fetchone() == (True,)
    try:
        with psycopg.connect(dsn, host="wrong.invalid", hostaddr="127.0.0.1",
                            sslmode="verify-full", sslrootcert=str(cert)):
            raise AssertionError("PostgreSQL accepted a mismatched verified hostname")
    except psycopg.OperationalError:
        pass


def cluster(action):
    cmd = [str(Path(os.environ["SMACKEND_POSTGRES_BIN"]) / "pg_ctl"), "-D", os.environ["SMACKEND_QUAL_PG_DATA"],
           "-w", action]
    if action in {"restart", "stop"}:
        cmd += ["-m", "fast" if action == "restart" else "immediate"]
    if action in {"restart", "start"}:
        cmd += ["-l", str(Path(os.environ["SMACKEND_QUAL_PG_DATA"]).parent / "postgres.log")]
    if action == "start":
        port = psycopg.conninfo.conninfo_to_dict(os.environ["SMACKEND_SMOKE_DSN"])["port"]
        socket = os.environ["SMACKEND_QUAL_PG_SOCKET"]
        cmd += ["-o", f"-h 127.0.0.1 -k {socket} -p {port} -c max_connections=50"]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, timeout=25)


@contextmanager
def app(project, env, log, tls=None):
    argv = [sys.executable, "-m", "uvicorn", "config.asgi:application", "--port", str(8446 if tls else PORT), "--host", "127.0.0.1",
            "--workers", "2", "--lifespan", "on", "--no-access-log", "--forwarded-allow-ips", "127.0.0.1"]
    if tls:
        cert, key = tls
        argv += ["--ssl-certfile", str(cert), "--ssl-keyfile", str(key)]
    with process(argv, folder=project, env=env, log=log) as server:
        yield server


def wait_until(check, seconds=25):
    start = monotonic()
    while monotonic() - start < seconds:
        try:
            if check():
                return monotonic() - start
        except httpx.HTTPError:
            pass
        sleep(0.1)
    raise AssertionError("Service did not reach expected state.")


def workers(pid):
    active = set()
    for child in psutil.Process(pid).children():
        try:
            if any("--multiprocessing-fork" in arg for arg in child.cmdline()):
                active.add(child.pid)
        except psutil.NoSuchProcess:
            continue
    return active


def dump_and_restore(source, destination, scratch):
    tools = Path(os.environ["SMACKEND_POSTGRES_BIN"])
    env = {**source, "PGHOST": source["POSTGRES_HOST"], "PGPORT": source["POSTGRES_PORT"],
           "PGUSER": source["POSTGRES_USER"], "PGPASSWORD": source["POSTGRES_PASSWORD"],
           "PGSSLMODE": "verify-full", "PGSSLROOTCERT": source["POSTGRES_SSLROOTCERT"]}
    dump = scratch / "synthetic.dump"
    subprocess.run([str(tools / "pg_dump"), "-Fc", "--no-owner", "--file", str(dump), source["POSTGRES_DB"]],
                   env=env, check=True, timeout=40, stdout=subprocess.DEVNULL)
    subprocess.run([str(tools / "pg_restore"), "--exit-on-error", "--no-owner", "--dbname",
                    destination["POSTGRES_DB"], str(dump)], env=env, check=True, timeout=40, stdout=subprocess.DEVNULL)


@contextmanager
def download_traffic(path, tls, cookies):
    stop = Event()
    samples = []
    stage = ["warm"]

    def load():
        with httpx.Client(base_url=TLS_ORIGIN, verify=tls, trust_env=False, timeout=8,
                          cookies=cookies, headers={"Host": PROXY_HOST}) as client:
            while not stop.is_set():
                phase = stage[0]
                try:
                    result = client.get(path)
                    outcome = "bad_body" if result.status_code == 200 and result.content != b"synthetic" else str(result.status_code)
                except httpx.HTTPError:
                    outcome = "transport_error"
                samples.append((phase, outcome))
                stop.wait(0.1)

    threads = [Thread(target=load) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        wait_until(lambda: sum(result == "200" for _, result in samples) >= 4)
        yield stage, samples
    finally:
        stop.set()
        for thread in threads:
            thread.join()


async def verify_wss(cookie, db, tls):
    uri = f"wss://{PROXY_HOST}:8446/ws/realtime/"
    parameters = {"host": "127.0.0.1", "ssl": tls, "origin": BROWSER_ORIGIN,
                  "additional_headers": {"Cookie": f"sessionid={cookie}"}, "proxy": None}

    def revoke():
        with psycopg.connect(os.environ["SMACKEND_SMOKE_DSN"], dbname=db) as connection:
            assert connection.execute("DELETE FROM django_session WHERE session_key = %s", (cookie,)).rowcount == 1
        return monotonic()

    async with connect(uri, **parameters) as socket:
        payload = json.dumps({"type": "echo", "text": "qualification"})
        await socket.send(payload)
        assert json.loads(await socket.recv()) == json.loads(payload)
        committed = await asyncio.to_thread(revoke)
        last_echo = None
        try:
            async with asyncio.timeout(5):
                while True:
                    await socket.send(payload)
                    assert json.loads(await socket.recv()) == json.loads(payload)
                    last_echo = monotonic()
                    await asyncio.sleep(0.05)
        except ConnectionClosed as exc:
            assert exc.rcvd is not None and exc.rcvd.code == 4403
        close_ms = (monotonic() - committed) * 1000
        assert close_ms <= 5000
    try:
        async with connect(uri, **parameters):
            raise AssertionError("A revoked session reconnected")
    except InvalidStatus as exc:
        assert exc.response.status_code == 403
    return {"close_after_commit_ms": close_ms,
            "last_echo_after_commit_ms": None if last_echo is None else (last_echo - committed) * 1000,
            "reconnect_status": 403}


def exercise_outage(browser, credentials):
    acquired, commit = Event(), Event()
    samples = []
    dsn = os.environ["SMACKEND_SMOKE_DSN"]

    def writer():
        try:
            with psycopg.connect(dsn, dbname=credentials["POSTGRES_DB"]) as connection:
                connection.execute("INSERT INTO qualification_write (operation) VALUES ('interrupted')")
                acquired.set()
                assert commit.wait(timeout=20)
                connection.commit()
        except psycopg.OperationalError:
            return True
        return False

    with psycopg.connect(dsn, dbname=credentials["POSTGRES_DB"]) as connection:
        connection.execute("CREATE TABLE qualification_write (operation text primary key)")
        connection.execute("INSERT INTO qualification_write VALUES ('committed')")
    with ThreadPoolExecutor() as pool:
        interrupted = pool.submit(writer)
        assert acquired.wait(timeout=10)
        cluster("stop")
        try:
            commit.set()
            assert interrupted.result(timeout=10)
            start = monotonic()
            while monotonic() - start < 30:
                live = browser.get("/api/health").status_code
                health = browser.get("/api/ready").status_code
                assert live == 200 and health == 503
                samples.append([live, health])
                sleep(0.2)
        finally:
            cluster("start")
    recovered = wait_until(lambda: browser.get("/api/ready").status_code == 200)
    with psycopg.connect(dsn, dbname=credentials["POSTGRES_DB"]) as connection:
        assert connection.execute("SELECT operation FROM qualification_write").fetchall() == [("committed",)]
    assert browser.get("/api/account/profile").status_code == 200
    return {"outage_samples": samples, "recovery_after_database_started_ms": recovered * 1000,
            "interrupted_write": "rolled back", "committed_write": "preserved"}


def main(scratch):
    scratch.mkdir(parents=True, exist_ok=False)
    cert, key = scratch / "test-cert.pem", scratch / "test-key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", "/CN=phase4-test", "-addext", "subjectAltName=IP:127.0.0.1,DNS:qualification.example.test",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    key.chmod(0o600)
    configure_postgresql_tls(cert, key)
    cli = ("-c", "from smackend.cli import main; raise SystemExit(main())")
    with database() as credentials, database() as restored, proxy(cert, key) as tls:
        env = credentials | {"DJANGO_ENV": "production", "DJANGO_DEBUG": "false",
                             "DJANGO_ALLOWED_HOSTS": PROXY_HOST, "DJANGO_TRUST_PROXY_SSL_HEADER": "true",
                             "POSTGRES_SSLMODE": "verify-full", "POSTGRES_SSLROOTCERT": str(cert),
                             "POSTGRES_POOL_TIMEOUT_SECONDS": "1", "SMACKEND_LOG_LEVEL": "WARNING",
                             "SMACKEND_REALTIME_ORIGINS": BROWSER_ORIGIN}
        run(scratch, env, *cli, "init", "deployment", "--storage", "--realtime", "--http")
        project = scratch / "deployment"
        run(project, env, "manage.py", "migrate", "--noinput")
        run(project, env, *cli, "verify")
        with app(project, env, scratch / "app.log") as runtime, httpx.Client(
            base_url=TLS_ORIGIN, verify=tls, timeout=10, trust_env=False, headers={"Host": PROXY_HOST},
        ) as browser:
            wait_until(lambda: browser.get("/api/ready").status_code == 200)
            with httpx.Client(trust_env=False) as client:
                redirect = client.get(f"http://127.0.0.1:{PORT}/api/health", headers={"Host": PROXY_HOST})
                assert redirect.status_code == 301
            start = browser.get("/identity/browser/v1/auth/session")
            assert "Secure" in start.headers["Set-Cookie"]
            def csrf():
                return {"X-CSRFToken": browser.cookies["csrftoken"], "Origin": BROWSER_ORIGIN}
            signup = browser.post("/identity/browser/v1/auth/signup", headers=csrf(),
                                  json={"username": "deployment", "password": PASSWORD})
            assert signup.status_code == 200
            session_header = next(value for value in signup.headers.get_list("Set-Cookie") if value.startswith("sessionid="))
            assert all(attribute in session_header for attribute in ("Secure", "HttpOnly", "SameSite=Lax"))
            denied = browser.post("/api/files/", files={"files": ("fixture.txt", b"synthetic", "text/plain")})
            assert denied.status_code == 403
            uploaded = browser.post("/api/files/", files={"files": ("fixture.txt", b"synthetic", "text/plain")},
                                    headers=csrf())
            assert uploaded.status_code == 201
            path = "/api/files/" + uploaded.json()[0]["id"] + "/download"
            assert browser.get(path).content == b"synthetic"
            with download_traffic(path, tls, httpx.Cookies(browser.cookies)) as (stage, samples):
                def both_workers():
                    return len(workers(runtime.pid)) == 2

                wait_until(both_workers)
                original = workers(runtime.pid)
                stage[0] = "replacement"
                os.kill(min(original), signal.SIGKILL)
                replacement = wait_until(lambda: both_workers() and workers(runtime.pid) != original)
                assert browser.get(path).content == b"synthetic"
                stage[0] = "outage_and_recovery"
                outage = exercise_outage(browser, env)
                assert browser.get(path).content == b"synthetic"
                stage[0] = "revocation"
                with httpx.Client(base_url=TLS_ORIGIN, verify=tls, trust_env=False, timeout=10,
                                  headers={"Host": PROXY_HOST}) as second:
                    second.get("/identity/browser/v1/auth/session")
                    signed_in = second.post("/identity/browser/v1/auth/signup",
                                            headers={"X-CSRFToken": second.cookies["csrftoken"], "Origin": BROWSER_ORIGIN},
                                            json={"username": "revocation", "password": PASSWORD})
                    assert signed_in.status_code == 200
                    with app(project, env, scratch / "wss.log", tls=(cert, key)):
                        with httpx.Client(verify=tls, trust_env=False, headers={"Host": PROXY_HOST}) as direct:
                            wait_until(lambda: direct.get("https://127.0.0.1:8446/api/ready").status_code == 200)
                        wss = asyncio.run(verify_wss(second.cookies["sessionid"], env["POSTGRES_DB"], tls))
            traffic = {label: dict(Counter(outcome for phase, outcome in samples if phase == label))
                       for label in {phase for phase, _ in samples}}
            assert all("bad_body" not in outcomes for outcomes in traffic.values())
            dump_and_restore(env, restored, scratch)
            cookies = httpx.Cookies(browser.cookies)
        storage = scratch / "restored-media"
        shutil.copytree(project / ".smackend-media", storage)
        restore = env | {"POSTGRES_DB": restored["POSTGRES_DB"], "SMACKEND_STORAGE_ROOT": str(storage)}
        with app(project, restore, scratch / "restore.log"), httpx.Client(
            base_url=TLS_ORIGIN, verify=tls, trust_env=False, timeout=10, cookies=cookies, headers={"Host": PROXY_HOST},
        ) as browser:
            wait_until(lambda: browser.get("/api/ready").status_code == 200)
            assert browser.get(path).content == b"synthetic"
            assert browser.get("/api/account/profile").json()["username"] == "deployment"
    print(json.dumps({"postgres_verify_full": True, "postgres_bad_hostname_rejected": True,
                      "secure_cookies_and_csrf": True, "database_and_file_restore": True,
                      "worker_replacement_ms": replacement * 1000, "download_traffic": traffic,
                      "direct_wss": wss, **outage}, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
