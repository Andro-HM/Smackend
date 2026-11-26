"""Run Django's native checks and tests with named, bounded verification steps."""

from copy import deepcopy
import os
import subprocess
import sys
from uuid import uuid4

STEPS = (
    ("configuration", "Configuration", "Review .env, module flags and credentials, and installed extras. Run python manage.py check for local details."),
    ("checks", "Django system checks", "Run python manage.py check to see the reported checks."),
    ("connection", "PostgreSQL connectivity", "Check PostgreSQL availability and the project's connection settings."),
    ("drift", "Model migration drift", "Run python manage.py makemigrations --check --dry-run and review the changes."),
    ("migrations", "Applied migrations", "Review python manage.py migrate --plan and apply approved migrations yourself."),
    ("tests", "Application and core tests", "Run python manage.py test for details. Check permissions to create test databases."),
)
POSTGRESQL = "django.db.backends.postgresql"


def verify_project(project, settings_module, labels):
    if not (project / "manage.py").is_file():
        print("Run smackend verify from the directory containing manage.py.", file=sys.stderr)
        return 2
    environ = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": settings_module or os.environ.get("DJANGO_SETTINGS_MODULE") or "config.settings",
    }
    for step, title, advice in STEPS:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "smackend.verify", step, *labels],
                env=environ, cwd=project, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=120,
            )
        except subprocess.TimeoutExpired:
            print(f"FAIL {title} (timed out). {advice}", file=sys.stderr)
            return 1
        except OSError:
            print(f"FAIL {title} (could not start Python). {advice}", file=sys.stderr)
            return 1
        if result.returncode:
            print(f"FAIL {title}. {advice}", file=sys.stderr)
            return 1
        print(f"PASS {title}", flush=True)
    return 0


def prepare_test_settings(settings):
    """Use fresh, tool-owned names regardless of application TEST.NAME overrides."""
    databases = deepcopy(settings.DATABASES)
    run_id = uuid4().hex
    for index, config in enumerate(databases.values()):
        if config["ENGINE"] != POSTGRESQL:
            raise ValueError("Verification requires the native PostgreSQL backend for every alias")
        test = config.setdefault("TEST", {})
        if not test.get("MIRROR"):
            test["NAME"] = f"smackend_verify_{run_id}_{index}"
    settings.DATABASES = databases


def require_unused_test_databases(connections):
    for alias in connections:
        db = connections[alias]
        settings = db.settings_dict
        if settings["TEST"].get("MIRROR"):
            continue
        try:
            with db.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [settings["TEST"]["NAME"]])
                if cursor.fetchone() is not None:
                    raise ValueError("The generated test database name is already in use")
        finally:
            db.close()
            # Django changes NAME during test setup. An existing pool retains its old connection parameters.
            db.close_pool()


def run_step(step, labels):
    import django
    from django.apps import apps
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connections

    if step == "configuration":
        if not settings.SECRET_KEY or settings.DATABASES["default"]["ENGINE"] != POSTGRESQL:
            raise ValueError("Django requires a secret key and the native PostgreSQL backend")
        return
    if step == "tests":
        prepare_test_settings(settings)

    django.setup()
    if step == "checks":
        call_command("check", fail_level="ERROR")
    elif step == "connection":
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                raise RuntimeError("Unexpected PostgreSQL probe response")
    elif step == "drift":
        # Including each app explicitly also detects missing initial migrations.
        app_labels = [app.label for app in apps.get_app_configs()
                      if settings.MIGRATION_MODULES.get(app.label, True) is not None]
        call_command("makemigrations", *app_labels, "--check", "--dry-run", "--noinput")
    elif step == "migrations":
        call_command("migrate", "--check", "--noinput")
    elif step == "tests":
        require_unused_test_databases(connections)
        # Preserve Django's confirmation guard if a test DB name is taken after the probe.
        # The parent supplies EOF on stdin, so the collision fails without dropping that DB.
        call_command("test", *(labels or ["."]), "smackend.verification_contracts", interactive=True)
    else:
        raise ValueError("Unknown verification step")


if __name__ == "__main__":
    run_step(sys.argv[1], sys.argv[2:])
