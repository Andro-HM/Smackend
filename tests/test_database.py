"""Run PostgreSQL checks in the generated project's Python environment."""

import os
from pathlib import Path
import subprocess

import pytest

from smackend.scaffold import create_project

PROBE = Path(__file__).with_name("database_probe.py").resolve()


@pytest.mark.parametrize("scenario", ["transactions", "workers", "restart"])
def test_database_behavior(tmp_path, scenario):
    python = os.environ.get("SMACKEND_SMOKE_PYTHON")
    requirement = "SMACKEND_POSTGRES_BIN" if scenario == "restart" else "SMACKEND_SMOKE_DSN"
    if not python or not os.environ.get(requirement):
        pytest.skip(f"Supply SMACKEND_SMOKE_PYTHON and {requirement} for the PostgreSQL {scenario} probe")
    project = create_project(tmp_path, f"database-{scenario}")
    environ = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("DJANGO_", "POSTGRES_", "PG"))
    }
    result = subprocess.run(
        [python, str(PROBE), scenario], cwd=project, env=environ, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout, end="")
