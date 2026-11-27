"""Read the project interpreter's Django settings and migration state."""

from importlib.metadata import version
from importlib.resources import files
import json
import os
from pathlib import Path
import sys

from .modules import MODULES
from .verify import POSTGRESQL, run_step


def managed_migration(migration, config, settings):
    if migration.app_label in settings.MIGRATION_MODULES or config is None:
        return False
    if config.name.startswith(("allauth.", "smackend.")) or config.name in {
        "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    }:
        return True
    if config.name != "accounts":
        return False
    relative = f"migrations/{migration.name}.py"
    template = files("smackend").joinpath("template", "accounts", *relative.split("/"))
    project_file = Path(config.path) / relative
    return template.is_file() and project_file.is_file() and template.read_bytes() == project_file.read_bytes()


def checks(action, expected):
    import django
    from django.apps import apps
    from django.conf import settings
    from django.core.management import call_command
    from django.db import OperationalError, connections
    from django.db.migrations.executor import MigrationExecutor

    result = {"version": version("smackend")}
    phase = "Django configuration or system checks failed. Run uv run python manage.py check for details."
    try:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        django.setup()
        enabled = {module.name for module in MODULES if getattr(settings, module.setting, False)}
        result["enabled"] = sorted(enabled)
        if action != "status" and enabled != expected:
            return result | {"error": "Django settings did not activate the planned modules. Review custom settings and environment overrides."}
        if settings.DATABASES["default"]["ENGINE"] != POSTGRESQL:
            return result | {"error": "Activation requires the native Django PostgreSQL backend."}
        call_command("check", fail_level="ERROR", verbosity=0)
        if action == "check":
            return result
        connection = connections["default"]
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except OperationalError:
            return result | {"offline": True}
        phase = "Migration state or model drift check failed. Inspect uv run python manage.py migrate --plan and makemigrations --check --dry-run."
        executor = MigrationExecutor(connection)
        executor.loader.check_consistent_history(connection)
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
        if action == "status":
            return result | {"pending": bool(plan)}
        by_label = {config.label: config for config in apps.get_app_configs()}
        if any(backward or not managed_migration(migration, by_label.get(migration.app_label), settings)
               for migration, backward in plan):
            return result | {"custom": True}
        if plan:
            call_command("migrate", interactive=False, verbosity=0)
        call_command("migrate", check_unapplied=True, interactive=False, verbosity=0)
        run_step("drift", ())
        return result | {"pending": False}
    except (Exception, SystemExit):
        return result | {"error": phase}


if __name__ == "__main__":
    print("SMACKEND_ACTIVATION " + json.dumps(checks(sys.argv[1], set(sys.argv[2:]))))
