import os
from pathlib import Path
import subprocess

import pytest

from smackend.scaffold import create_project


PROBE = Path(__file__).with_name("modules_probe.py").resolve()


@pytest.mark.parametrize("scenario", ["storage", "s3", "admin", "admin-storage", "http", "plain", "upgrade"])
def test_modules_against_postgres(tmp_path, scenario):
    python = os.environ.get("SMACKEND_MODULE_SMOKE_PYTHON")
    if not python or not os.environ.get("SMACKEND_SMOKE_DSN"):
        pytest.skip("Supply SMACKEND_MODULE_SMOKE_PYTHON and a disposable PostgreSQL SMACKEND_SMOKE_DSN")
    project = create_project(tmp_path, f"check-{scenario}", storage=scenario in {"storage", "s3", "admin-storage"},
                             admin=scenario.startswith("admin"), http=scenario == "http", auth=scenario == "upgrade")
    environ = {key: value for key, value in os.environ.items()
               if not key.startswith(("DJANGO_", "POSTGRES_", "PG", "SMACKEND_AUTH_", "SMACKEND_STORAGE_",
                                      "SMACKEND_ADMIN_", "SMACKEND_HTTP_"))}
    environ["SMACKEND_SMOKE_DSN"] = os.environ["SMACKEND_SMOKE_DSN"]
    process = subprocess.run([python, str(PROBE), scenario], cwd=project, env=environ,
                             text=True, capture_output=True, timeout=120)
    assert process.returncode == 0, process.stdout + process.stderr
