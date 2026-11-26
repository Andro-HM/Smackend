import os
from pathlib import Path
import subprocess

import pytest

from smackend.scaffold import create_project


PROBE = Path(__file__).with_name("identity_probe.py").resolve()


@pytest.mark.parametrize("scenario", [
    "username", "email-password", "phone-password", "email-otp", "phone-otp", "mixed", "optional",
    "challenges", "phone-race", "google", "workers", "authorization", "database-failure",
])
def test_postgresql_identity(tmp_path, scenario):
    python = os.environ.get("SMACKEND_AUTH_SMOKE_PYTHON")
    if not python or not os.environ.get("SMACKEND_SMOKE_DSN"):
        pytest.skip("Supply SMACKEND_AUTH_SMOKE_PYTHON and a disposable PostgreSQL SMACKEND_SMOKE_DSN")
    project = create_project(tmp_path, f"identity-{scenario}", auth=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("DJANGO_", "POSTGRES_", "PG", "SMACKEND_AUTH_"))}
    env["SMACKEND_SMOKE_DSN"] = os.environ["SMACKEND_SMOKE_DSN"]
    result = subprocess.run([python, str(PROBE), scenario], cwd=project, env=env,
                            capture_output=True, text=True, timeout=140)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout, end="")
