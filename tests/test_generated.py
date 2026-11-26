"""Opt-in smoke test for a generated project using a disposable PostgreSQL database."""

import os
import subprocess

import pytest

from smackend.scaffold import create_project

SMOKE = r"""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg
from psycopg import sql

dsn = os.environ["SMACKEND_SMOKE_DSN"]
credentials = psycopg.conninfo.conninfo_to_dict(dsn)
loopback = {"127.0.0.1", "localhost", "::1"}
assert credentials.get("host") in loopback, "Use a local disposable test cluster"
assert credentials.get("hostaddr", credentials["host"]) in loopback, "Use a local disposable test cluster"
assert credentials.get("password"), "Pass a password in the smoke DSN"
database = "smackend_phase11_" + uuid4().hex
with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as admin:
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        os.environ.update(
            POSTGRES_DB=database, POSTGRES_HOST=admin.info.host,
            POSTGRES_PORT=str(admin.info.port), POSTGRES_USER=admin.info.user,
            POSTGRES_PASSWORD=credentials["password"],
        )
        for command in (["check"], ["migrate", "--noinput"], ["migrate", "--check"]):
            subprocess.run([sys.executable, "manage.py", *command], check=True, timeout=20)
        subprocess.run([sys.executable, "manage.py", "makemigrations", "--check", "--dry-run"], check=True, timeout=20)

        # A consuming app owns its models and receives a swappable user dependency.
        app = Path("inventory")
        app.mkdir()
        (app / "__init__.py").touch()
        base_model = '''from django.conf import settings
from django.db import models
class Item(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    name = models.CharField(max_length=40)
    class Meta:
        constraints = [models.UniqueConstraint(fields=("owner", "name"), name="inventory_unique_name")]
'''
        (app / "models.py").write_text(base_model, encoding="utf-8")
        with Path("config/settings.py").open("a", encoding="utf-8") as settings_file:
            settings_file.write('\nINSTALLED_APPS += ["inventory"]\n')

        def manage(*args, expect=0):
            result = subprocess.run([sys.executable, "manage.py", *args], capture_output=True, text=True, timeout=20)
            assert result.returncode == expect, result.stdout + result.stderr
            return result.stdout

        def verify(*args, expect=0, **overrides):
            command = ("import sys; from smackend.cli import main; "
                       "raise SystemExit(main(['verify', *sys.argv[1:]]))")
            result = subprocess.run(
                [sys.executable, "-c", command, *args], capture_output=True, text=True,
                env={**os.environ, **overrides}, timeout=60,
            )
            assert result.returncode == expect, result.stdout + result.stderr
            return result.stdout + result.stderr

        manage("makemigrations", "inventory", "--check", "--dry-run", expect=1)
        drift_result = verify(expect=1)
        assert "FAIL Model migration drift" in drift_result, drift_result
        manage("makemigrations", "inventory")
        initial = next((app / "migrations").glob("0001_*.py")).read_text(encoding="utf-8")
        assert "migrations.swappable_dependency(settings.AUTH_USER_MODEL)" in initial
        manage("migrate", "--check", expect=1)
        pending_result = verify(expect=1)
        assert "FAIL Applied migrations" in pending_result, pending_result
        manage("migrate", "--noinput")
        with psycopg.connect(dsn, dbname=database) as fixture:
            fixture.execute("INSERT INTO accounts_user (password, is_superuser, username, first_name, last_name, email, is_staff, is_active, date_joined) VALUES ('!', false, 'owner', '', '', '', false, true, now())")
            fixture.execute("INSERT INTO inventory_item (owner_id, name) SELECT id, 'retained' FROM accounts_user")
            order = fixture.execute("SELECT app FROM django_migrations WHERE app IN ('accounts', 'inventory') ORDER BY id").fetchall()
            assert order == [("accounts",), ("accounts",), ("inventory",)]
        (app / "models.py").write_text(
            base_model.replace("    class Meta:", "    flag = models.BooleanField(default=False)\n    class Meta:"),
            encoding="utf-8",
        )
        manage("makemigrations", "--check", "--dry-run", expect=1)
        manage("makemigrations", "inventory")
        manage("migrate", "inventory", "--noinput")
        manage("migrate", "inventory", "0001", "--noinput")
        with psycopg.connect(dsn, dbname=database) as fixture:
            assert fixture.execute("SELECT name FROM inventory_item").fetchall() == [("retained",)]
            assert not fixture.execute("SELECT 1 FROM information_schema.columns WHERE table_name='inventory_item' AND column_name='flag'").fetchall()
        manage("migrate", "inventory", "--noinput")
        manage("makemigrations", "--check", "--dry-run")

        (app / "api.py").write_text('''
from typing import Annotated
from django.core.exceptions import PermissionDenied
from ninja import Field, FilterLookup, FilterSchema, Query, Router, Schema
from ninja.errors import Throttled
from ninja.pagination import paginate
from smackend.errors import APIError
from .models import Item

router = Router()

class ItemOutput(Schema):
    id: int
    name: str

class ItemFilter(FilterSchema):
    name: Annotated[str | None, FilterLookup("name__icontains")] = Field(None, max_length=40)

@router.get("/", response=list[ItemOutput])
@paginate
def list_items(request, filters: Query[ItemFilter]):
    return filters.filter(Item.objects.all()).order_by("id")

@router.get("/count", response=int)
async def item_count(request):
    return await Item.objects.acount()

@router.get("/denied")
def denied(request):
    raise PermissionDenied("private-error-marker")

@router.get("/broken", response=ItemOutput)
async def broken_output(request):
    return {"name": "private-error-marker"}

@router.get("/identified-error")
def identified_error(request):
    raise APIError(401, "sign_in_required", "Sign in to continue.",
                   headers={"WWW-Authenticate": "Bearer"})

@router.get("/throttled")
def throttled(request):
    raise Throttled(3)

@router.post("/duplicate", response=ItemOutput)
def duplicate(request):
    existing = Item.objects.first()
    return Item.objects.create(name=existing.name, owner=existing.owner)
''', encoding="utf-8")
        with Path("config/api.py").open("a", encoding="utf-8") as api_file:
            api_file.write('\nfrom inventory.api import router as inventory_router\napi.add_router("/inventory", inventory_router)\n')

        (app / "tests.py").write_text('''
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from .models import Item

class InventoryTests(TestCase):
    def test_application_feature(self):
        self.assertTrue(connection.settings_dict["NAME"].startswith("smackend_verify_"))
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            self.assertEqual(cursor.fetchone()[0], connection.settings_dict["NAME"])
        self.assertFalse(Item.objects.exists())
        user = get_user_model().objects.create_user(username="app-test")
        Item.objects.create(owner=user, name="feature")
        self.assertEqual(Item.objects.get().owner, user)
''', encoding="utf-8")
        # Even a pre-existing TEST.NAME pointing to the live name must be ignored by verify.
        with Path("config/settings.py").open("a", encoding="utf-8") as settings_file:
            settings_file.write('\nDATABASES["default"]["TEST"] = {"NAME": DATABASES["default"]["NAME"]}\n')
        invalid_config = verify(expect=1, POSTGRES_PORT="private-bad-port")
        assert "FAIL Configuration" in invalid_config, invalid_config
        verification = verify()
        assert "PASS Application and core tests" in verification, verification
        test_file = app / "tests.py"
        original_tests = test_file.read_text(encoding="utf-8")
        test_file.write_text(original_tests.replace("self.assertEqual(Item.objects.get().owner, user)",
                                                  "self.fail('private-app-test-marker')"), encoding="utf-8")
        failed_tests = verify("inventory", expect=1)
        assert "FAIL Application and core tests" in failed_tests and "private-app-test-marker" not in failed_tests
        test_file.write_text(original_tests, encoding="utf-8")

        def exercise_server(expect_ready):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            origin = f"http://127.0.0.1:{port}"
            command = [sys.executable, "-m", "uvicorn", "config.asgi:application", "--host", "127.0.0.1",
                       "--port", str(port), "--lifespan", "off", "--no-proxy-headers", "--no-access-log"]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            with Path("smoke-uvicorn.log").open("w+", encoding="utf-8") as log:
                server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
                try:
                    for _ in range(80):
                        try:
                            with urlopen(origin + "/api/health", timeout=1) as response:
                                assert json.load(response) == {"status": "ok"}
                            break
                        except OSError:
                            if server.poll() is not None:
                                raise RuntimeError("Uvicorn exited")
                            time.sleep(0.1)
                    else:
                        raise RuntimeError("Uvicorn never became reachable")
                    if expect_ready:
                        secret = "private-log-marker"
                        health_request = Request(origin + "/api/health?credential=" + secret, headers={
                            "Authorization": "Bearer " + secret, "Cookie": "sessionid=" + secret,
                            "X-Request-ID": secret,
                        })
                        with urlopen(health_request, timeout=5) as response:
                            health_id = response.headers["X-Request-ID"]
                            assert re.fullmatch(r"[0-9a-f]{32}", health_id)
                        with urlopen(origin + "/api/ready", timeout=5) as response:
                            assert json.load(response) == {"status": "ready"}
                        with urlopen(origin + "/", timeout=5) as response:
                            assert b"postgres-smoke" in response.read()
                        for route, mime in (("styles.css", "text/css"), ("app.js", "text/javascript")):
                            with urlopen(origin + "/assets/" + route, timeout=5) as response:
                                assert response.headers.get_content_type() == mime
                        with urlopen(origin + "/api/openapi.json", timeout=5) as response:
                            assert {"/api/ready", "/api/inventory/", "/api/inventory/count"} <= json.load(response)["paths"].keys()
                        with urlopen(origin + "/api/inventory/?name=tain&limit=1", timeout=5) as response:
                            data = json.load(response)
                            assert data["count"] == 1 and data["items"][0]["name"] == "retained"
                        with urlopen(origin + "/api/inventory/count", timeout=5) as response:
                            assert json.load(response) == 1
                        cases = (
                            ("GET", "/api/inventory/?limit=private-error-marker", 422, "validation_error", None),
                            ("GET", "/api/inventory/denied", 403, "forbidden", None),
                            ("GET", "/api/inventory/broken", 500, "internal_error", None),
                            ("GET", "/api/inventory/identified-error", 401, "sign_in_required", ("WWW-Authenticate", "Bearer")),
                            ("GET", "/api/inventory/throttled", 429, "rate_limited", ("Retry-After", "3")),
                            ("DELETE", "/api/inventory/count", 405, "method_not_allowed", ("Allow", "GET")),
                            ("GET", "/api/no-such-route", 404, "not_found", None),
                            ("POST", "/api/inventory/duplicate", 409, "database_conflict", None),
                        )
                        for method, path, status, code, header in cases:
                            try:
                                urlopen(Request(origin + path, method=method), timeout=5).close()
                            except HTTPError as error:
                                payload = json.load(error)
                                assert error.code == status and payload["code"] == code, (path, payload)
                                assert payload["message"] and "private-error-marker" not in str(payload)
                                assert error.headers["X-Request-ID"] == payload["request_id"]
                                if header:
                                    assert error.headers[header[0]] == header[1]
                            else:
                                raise AssertionError(f"{path} did not return its expected error")
                        raw_logs = Path("smoke-uvicorn.log").read_text(encoding="utf-8")
                        assert secret not in raw_logs and "private-error-marker" not in raw_logs
                        records = [json.loads(line) for line in raw_logs.splitlines() if line.startswith("{")]
                        health_log = next(record for record in records if record.get("request_id") == health_id)
                        assert health_log["event"] == "request.complete" and health_log["status"] == 200
                        assert health_log["duration_ms"] >= 0 and health_log["route"].endswith("health")
                        assert any(record["event"] == "api.exception" and record["status"] == 500 for record in records)
                    else:
                        try:
                            urlopen(origin + "/api/ready", timeout=8).close()
                        except HTTPError as error:
                            assert error.code == 503
                            error_body = json.load(error)
                            assert error_body["status"] == "unavailable"
                            assert error_body["code"] == "database_unavailable" and error_body["message"]
                        else:
                            raise AssertionError("Readiness succeeded for a missing PostgreSQL database")
                except Exception:
                    log.seek(0)
                    print(log.read())
                    raise
                finally:
                    if server.poll() is None:
                        server.terminate()
                        server.wait(timeout=10)

        exercise_server(expect_ready=True)
        os.environ["POSTGRES_DB"] = database + "_absent"
        exercise_server(expect_ready=False)

        # Exercise production middleware without implying the local DB or server has TLS.
        production = {
            **os.environ, "DJANGO_SETTINGS_MODULE": "config.settings", "DJANGO_ENV": "production",
            "DJANGO_DEBUG": "false", "DJANGO_ALLOWED_HOSTS": "app.example.test",
            "DJANGO_TRUST_PROXY_SSL_HEADER": "false", "POSTGRES_SSLMODE": "verify-full",
        }
        subprocess.run([sys.executable, "-c", '''
import django
django.setup()
from django.test import Client
browser = Client(HTTP_HOST="app.example.test")
redirect = browser.get("/api/health", HTTP_X_FORWARDED_PROTO="https")
assert redirect.status_code == 301
assert redirect["Location"] == "https://app.example.test/api/health"
secure = browser.get("/api/health", secure=True)
assert secure.status_code == 200
assert secure["Strict-Transport-Security"] == "max-age=300"
assert secure["X-Frame-Options"] == "DENY"
assert browser.get("/api/health", HTTP_HOST="unexpected.example.test", secure=True).status_code == 400
'''], env=production, check=True, timeout=20)
    finally:
        admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
"""


def test_generated_project_runs_with_postgresql(tmp_path):
    python = os.environ.get("SMACKEND_SMOKE_PYTHON")
    if not (python and os.environ.get("SMACKEND_SMOKE_DSN")):
        pytest.skip("Set SMACKEND_SMOKE_PYTHON and SMACKEND_SMOKE_DSN to run the PostgreSQL smoke")
    project = create_project(tmp_path, "postgres-smoke")
    environ = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("DJANGO_", "POSTGRES_"))
    }
    result = subprocess.run(
        [python, "-c", SMOKE], cwd=project, env=environ, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
