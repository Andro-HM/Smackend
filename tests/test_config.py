"""Exercise the actual settings files copied into a standalone project."""

import json
import os
from secrets import token_urlsafe
import subprocess
import sys

import pytest

from smackend.scaffold import create_project

LEGACY_ENV = {
    "DJANGO_SECRET_KEY": "legacy-test-value",
    "POSTGRES_DB": "legacy_notes",
    "POSTGRES_USER": "legacy_user",
    "POSTGRES_PASSWORD": "legacy-test-password",
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "55432",
}
PRODUCTION_ENV = {
    "DJANGO_ENV": "production",
    "DJANGO_SECRET_KEY": token_urlsafe(48),
    "DJANGO_ALLOWED_HOSTS": "App.Example.com,api.example.com",
    "DJANGO_CSRF_TRUSTED_ORIGINS": "https://App.Example.com:443,https://api.example.com:8443",
    "POSTGRES_DB": "prod_notes",
    "POSTGRES_USER": "prod_app",
    "POSTGRES_PASSWORD": token_urlsafe(32),
    "POSTGRES_HOST": "postgres.example.com",
}
SNAPSHOT = """
import json, os
from config import settings as s
db = s.DATABASES["default"]
print(json.dumps({
    "debug": s.DEBUG, "hosts": s.ALLOWED_HOSTS, "origins": s.CSRF_TRUSTED_ORIGINS,
    "ssl_redirect": s.SECURE_SSL_REDIRECT, "session_secure": s.SESSION_COOKIE_SECURE,
    "csrf_secure": s.CSRF_COOKIE_SECURE, "hsts": s.SECURE_HSTS_SECONDS, "proxy": s.SECURE_PROXY_SSL_HEADER,
    "engine": db["ENGINE"], "database": db["NAME"], "database_host": db["HOST"], "port": db["PORT"],
    "sslmode": db["OPTIONS"]["sslmode"], "sslrootcert": db["OPTIONS"].get("sslrootcert"),
    "gssencmode": db["OPTIONS"].get("gssencmode"),
    "pool": db["OPTIONS"]["pool"], "connect_timeout": db["OPTIONS"]["connect_timeout"],
    "pg_options": db["OPTIONS"]["options"],
    "connection_age": db["CONN_MAX_AGE"], "health_checks": db["CONN_HEALTH_CHECKS"],
    "log_level": s.LOGGING["root"]["level"],
    "key_matches": s.SECRET_KEY == os.environ.get("EXPECTED_KEY"),
    "password_matches": db["PASSWORD"] == os.environ.get("EXPECTED_PASSWORD"),
    "file_key_in_process": "POSTGRES_USER" in os.environ,
}))
"""


@pytest.fixture
def project(tmp_path):
    return create_project(tmp_path, "config-test")


def dotfile(project, values):
    (project / ".env").write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8",
    )


def import_settings(project, **process_variables):
    environ = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("DJANGO_", "POSTGRES_"))
    }
    environ.update(process_variables)
    return subprocess.run(
        [sys.executable, "-c", SNAPSHOT], cwd=project, env=environ, capture_output=True, text=True, timeout=10,
    )


def snapshot(project, **variables):
    result = import_settings(project, **variables)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_phase_11_dotenv_still_selects_development(project):
    dotfile(project, LEGACY_ENV)
    result = snapshot(project, EXPECTED_KEY=LEGACY_ENV["DJANGO_SECRET_KEY"])
    assert result["key_matches"]
    assert result["engine"] == "django.db.backends.postgresql"
    assert result["database"] == "legacy_notes" and result["port"] == 55432
    assert result["hosts"] == ["127.0.0.1", "localhost", "[::1]"]
    assert result["sslmode"] == "prefer"
    assert not any(result[key] for key in ("debug", "ssl_redirect", "session_secure", "csrf_secure"))
    assert result["proxy"] is None
    assert not result["file_key_in_process"]
    assert result["pool"] == {"min_size": 0, "max_size": 4, "timeout": 4}
    assert result["connect_timeout"] == 3
    assert result["connection_age"] == 0 and result["health_checks"]


def test_precedence_and_literal_credentials(project):
    dotfile(project, {**LEGACY_ENV, "POSTGRES_PORT": "55500", "POSTGRES_PASSWORD": "literal-${OTHER}-pass"})
    result = snapshot(project, POSTGRES_PORT="55333", EXPECTED_PASSWORD="literal-${OTHER}-pass")
    assert result["port"] == 55333 and result["password_matches"]
    assert snapshot(project)["port"] == 55500
    dotfile(project, {k: v for k, v in LEGACY_ENV.items() if k not in {"POSTGRES_PORT", "POSTGRES_HOST"}})
    assert snapshot(project)["port"] == 55432
    assert snapshot(project)["database_host"] == "127.0.0.1"
    supplied = "process-value-that-wins"
    assert snapshot(project, DJANGO_SECRET_KEY=supplied, EXPECTED_KEY=supplied)["key_matches"]


def test_environment_only_production(project):
    (project / ".env").unlink()
    result = snapshot(project, **PRODUCTION_ENV, POSTGRES_SSLROOTCERT="postgres-ca.pem")
    assert result["hosts"] == ["app.example.com", "api.example.com"]
    assert result["origins"] == ["https://app.example.com", "https://api.example.com:8443"]
    assert result["port"] == 5432 and result["sslmode"] == "verify-full"
    assert result["gssencmode"] == "disable"
    assert result["sslrootcert"] == "postgres-ca.pem"
    assert all(result[key] for key in ("ssl_redirect", "session_secure", "csrf_secure", "hsts"))
    assert not result["debug"] and result["proxy"] is None
    assert snapshot(project, **PRODUCTION_ENV, DJANGO_TRUST_PROXY_SSL_HEADER="1")["proxy"] == [
        "HTTP_X_FORWARDED_PROTO", "https",
    ]


def test_development_custom_hosts_and_debug(project):
    result = snapshot(
        project, DJANGO_DEBUG="true", DJANGO_ALLOWED_HOSTS="[::1],LAB.Example.com,[::1]",
        DJANGO_CSRF_TRUSTED_ORIGINS="http://localhost:8000,https://[::1]:443", POSTGRES_HOST="::1",
    )
    assert result["debug"] and not result["session_secure"]
    assert result["hosts"] == ["[::1]", "lab.example.com"]
    assert result["origins"] == ["http://localhost:8000", "https://[::1]"]
    assert result["database_host"] == "::1"


@pytest.mark.parametrize("key", ["DJANGO_SECRET_KEY", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"])
def test_required_values_missing_or_explicitly_blank(project, key):
    dotfile(project, {k: v for k, v in LEGACY_ENV.items() if k != key})
    missing = import_settings(project)
    assert missing.returncode != 0 and f"Set {key}" in missing.stderr
    dotfile(project, LEGACY_ENV)
    blank = import_settings(project, **{key: ""})
    assert blank.returncode != 0 and f"Set {key}" in blank.stderr


@pytest.mark.parametrize(("key", "value"), [
    ("DJANGO_ENV", "staging"), ("DJANGO_DEBUG", "maybe"), ("DJANGO_TRUST_PROXY_SSL_HEADER", "yes"),
    ("POSTGRES_PORT", "65536"), ("POSTGRES_PORT", "-1"), ("POSTGRES_PORT", "0"),
    ("POSTGRES_HOST", "https://db.example.com"), ("POSTGRES_HOST", "localhost:5432"),
    ("DJANGO_ALLOWED_HOSTS", "example.com:443"), ("DJANGO_ALLOWED_HOSTS", "*"),
    ("DJANGO_ALLOWED_HOSTS", "example.com,"), ("DJANGO_ALLOWED_HOSTS", "999.999.999.999"),
    ("DJANGO_CSRF_TRUSTED_ORIGINS", "https://app.example.com/private"),
    ("DJANGO_CSRF_TRUSTED_ORIGINS", "https://person:private-origin-secret@app.example.com"),
    ("DJANGO_CSRF_TRUSTED_ORIGINS", "https://[::1]unexpected"),
    ("DJANGO_CSRF_TRUSTED_ORIGINS", "https://app.example.com:70000"),
    ("DJANGO_CSRF_TRUSTED_ORIGINS", "https://app.example.com#"),
    ("POSTGRES_SSLMODE", "insecure-mode"),
])
def test_invalid_configuration_reports_only_the_key(project, key, value):
    result = import_settings(project, **{key: value})
    assert result.returncode != 0 and key in result.stderr
    assert "private-origin-secret" not in result.stderr


@pytest.mark.parametrize(("key", "value"), [
    ("DJANGO_DEBUG", "1"), ("DJANGO_SECRET_KEY", "private-short-key"),
    ("DJANGO_SECRET_KEY", "django-insecure-" + "AbCdEf" * 10),
    ("POSTGRES_PASSWORD", "private-short"),
    ("DJANGO_ALLOWED_HOSTS", "127.0.0.1"), ("DJANGO_ALLOWED_HOSTS", "0.0.0.0"),
    ("DJANGO_ALLOWED_HOSTS", "[::ffff:127.0.0.1]"),
    ("DJANGO_ALLOWED_HOSTS", ""), ("POSTGRES_HOST", ""),
    ("DJANGO_CSRF_TRUSTED_ORIGINS", "http://app.example.com"), ("POSTGRES_SSLMODE", "require"),
])
def test_production_rejects_unsafe_configuration(project, key, value):
    (project / ".env").unlink()
    result = import_settings(project, **{**PRODUCTION_ENV, key: value})
    assert result.returncode != 0 and key in result.stderr
    assert "private-short" not in result.stderr
    assert PRODUCTION_ENV["DJANGO_SECRET_KEY"] not in result.stderr
    assert PRODUCTION_ENV["POSTGRES_PASSWORD"] not in result.stderr


def test_malformed_dotenv_error_does_not_echo_content(project):
    (project / ".env").write_text("# local\nSECRET='private-unclosed-value\n", encoding="utf-8")
    result = import_settings(project, **PRODUCTION_ENV)
    assert result.returncode != 0
    assert "Invalid .env assignment on line 2" in result.stderr
    assert "private-unclosed-value" not in result.stderr


def test_database_settings_accept_valid_overrides(project):
    result = snapshot(
        project, POSTGRES_POOL_MIN_SIZE="1", POSTGRES_POOL_MAX_SIZE="6", POSTGRES_POOL_TIMEOUT_SECONDS="7",
        POSTGRES_CONNECT_TIMEOUT_SECONDS="8", POSTGRES_STATEMENT_TIMEOUT_MS="2000",
        POSTGRES_LOCK_TIMEOUT_MS="500", POSTGRES_IDLE_IN_TRANSACTION_TIMEOUT_MS="9000",
    )
    assert result["pool"] == {"min_size": 1, "max_size": 6, "timeout": 7}
    assert result["connect_timeout"] == 8
    assert result["pg_options"] == (
        "-c statement_timeout=2000 -c lock_timeout=500 -c idle_in_transaction_session_timeout=9000"
    )


@pytest.mark.parametrize(("key", "value"), [
    ("POSTGRES_POOL_MIN_SIZE", "5"), ("POSTGRES_POOL_MIN_SIZE", "-1"),
    ("POSTGRES_POOL_MAX_SIZE", "0"), ("POSTGRES_POOL_MAX_SIZE", "129"),
    ("POSTGRES_POOL_TIMEOUT_SECONDS", ""), ("POSTGRES_POOL_TIMEOUT_SECONDS", "1.5"),
    ("POSTGRES_CONNECT_TIMEOUT_SECONDS", "1"), ("POSTGRES_CONNECT_TIMEOUT_SECONDS", "301"),
    ("POSTGRES_STATEMENT_TIMEOUT_MS", "0"), ("POSTGRES_STATEMENT_TIMEOUT_MS", "2147483648"),
    ("POSTGRES_LOCK_TIMEOUT_MS", "-1"), ("POSTGRES_IDLE_IN_TRANSACTION_TIMEOUT_MS", "bad-value"),
])
def test_database_settings_reject_invalid_ranges(project, key, value):
    result = import_settings(project, **{key: value})
    assert result.returncode != 0 and key in result.stderr
    assert "bad-value" not in result.stderr


def test_configurable_log_levels(project):
    assert snapshot(project)["log_level"] == "INFO"
    assert snapshot(project, SMACKEND_LOG_LEVEL="ERROR")["log_level"] == "ERROR"
    failed = import_settings(project, SMACKEND_LOG_LEVEL="private-invalid-level")
    assert failed.returncode and "SMACKEND_LOG_LEVEL" in failed.stderr
    assert "private-invalid-level" not in failed.stderr
