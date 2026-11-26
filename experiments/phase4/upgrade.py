"""Verify an existing a7 application's sessions, rows and files with a8."""

import json
import subprocess
import sys
from pathlib import Path

from rehearsal import database


CLI = "from smackend.cli import main; raise SystemExit(main())"
SEED = """
import json
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.middleware.csrf import _get_new_csrf_string
from django.test import Client

user = get_user_model().objects.create_user(username="upgrade_fixture", first_name="Kept")
browser = Client(HTTP_HOST="127.0.0.1", enforce_csrf_checks=True)
browser.force_login(user)
token = _get_new_csrf_string()
browser.cookies["csrftoken"] = token
uploaded = browser.post("/api/files/", {"files": SimpleUploadedFile("fixture.txt", b"retained payload")},
                        HTTP_X_CSRFTOKEN=token)
assert uploaded.status_code == 201
Path("private-upgrade.json").write_text(json.dumps({"session": browser.cookies["sessionid"].value,
                                                  "id": uploaded.json()[0]["id"]}), encoding="utf-8")
"""
CHECK = """
import json
from pathlib import Path
from django.test import Client

fixture = json.loads(Path("private-upgrade.json").read_text(encoding="utf-8"))
browser = Client(HTTP_HOST="127.0.0.1", enforce_csrf_checks=True)
browser.cookies["sessionid"] = fixture["session"]
profile = browser.get("/api/account/profile")
assert profile.status_code == 200 and profile.json()["first_name"] == "Kept"
file = browser.get("/api/files/" + fixture["id"] + "/download")
assert file.status_code == 200 and b"".join(file.streaming_content) == b"retained payload"
file.close()
"""


def execute(python, path, env, *args):
    result = subprocess.run([str(python), *args], cwd=path, env=env, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def main(old_python, new_python, scratch):
    scratch.mkdir(parents=True, exist_ok=False)
    with database() as env:
        execute(old_python, scratch, env, "-c", CLI, "init", "upgrade-project", "--storage", "--ai", "--realtime")
        project = scratch / "upgrade-project"
        execute(old_python, project, env, "manage.py", "migrate", "--noinput")
        execute(old_python, project, env, "manage.py", "shell", "-c", SEED)
        versions = project / "pyproject.toml"
        versions.write_text(versions.read_text(encoding="utf-8").replace("==0.1.0a7", "==0.1.0a8"), encoding="utf-8")
        execute(new_python, project, env, "manage.py", "makemigrations", "--check", "--dry-run")
        execute(new_python, project, env, "manage.py", "migrate", "--noinput")
        execute(new_python, project, env, "manage.py", "shell", "-c", CHECK)
        verified = execute(new_python, project, env, "-c", CLI, "verify")
        print(json.dumps({"upgrade": "a7 -> a8", "existing_session": True, "existing_data": True,
                          "verified": verified.splitlines()}, indent=2))


if __name__ == "__main__":
    main(*(Path(entry).resolve() for entry in sys.argv[1:]))
