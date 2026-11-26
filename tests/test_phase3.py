import os
from pathlib import Path
import subprocess

import pytest

from smackend.scaffold import create_project


PROBE = Path(__file__).with_name("phase3_probe.py").resolve()


@pytest.mark.parametrize("mode", ["ai", "realtime", "combined", "disabled"])
def test_phase3_postgres(tmp_path, mode):
    python = os.environ.get("SMACKEND_PHASE3_SMOKE_PYTHON")
    if not python or not os.environ.get("SMACKEND_SMOKE_DSN"):
        pytest.skip("Supply the Phase 3 smoke interpreter and isolated PostgreSQL DSN")
    project = create_project(tmp_path, f"phase3-{mode}", ai=mode in {"ai", "combined"},
                             realtime=mode in {"realtime", "combined"}, storage=mode in {"ai", "combined"},
                             admin=mode in {"ai", "combined"})
    environ = {key: value for key, value in os.environ.items()
               if not key.startswith(("DJANGO_", "POSTGRES_", "PG", "SMACKEND_AUTH_", "SMACKEND_AI_",
                                      "SMACKEND_HTTP_", "SMACKEND_REALTIME_", "SMACKEND_STORAGE_"))}
    result = subprocess.run([python, str(PROBE), mode], cwd=project, env=environ, capture_output=True,
                            text=True, timeout=130)
    assert result.returncode == 0, result.stdout + result.stderr
