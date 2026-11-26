from importlib.metadata import version
import os
from pathlib import Path
import stat
import tomllib

import pytest

from smackend import scaffold
from smackend.cli import main


def env_values(path):
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def test_init_writes_an_independent_project(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "private-notes"]) == 0
    project = tmp_path / "private-notes"
    expected = {
        ".env", ".env.example", ".gitignore", "README.md", "compose.yaml", "manage.py", "pyproject.toml",
        "config/__init__.py", "config/admin.py", "config/api.py", "config/asgi.py", "config/env.py",
        "config/settings.py", "config/urls.py",
        "frontend/index.html", "frontend/styles.css", "frontend/app.js",
        "frontend/account.html", "frontend/account.js", "frontend/assist.html", "frontend/assist.js",
        "accounts/__init__.py", "accounts/models.py", "accounts/migrations/__init__.py",
        "accounts/migrations/0001_initial.py", "accounts/migrations/0002_username_case_insensitive.py",
    }
    assert {path.relative_to(project).as_posix() for path in project.rglob("*") if path.is_file()} == expected
    output = capsys.readouterr().out
    assert "manage.py migrate" in output
    project_metadata = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project_metadata["name"] == "private-notes"
    assert f"smackend[backend]=={version('smackend')}" in project_metadata["dependencies"]
    assert "private-notes" in (project / "frontend/index.html").read_text(encoding="utf-8")
    example = env_values(project / ".env.example")
    env = env_values(project / ".env")
    assert env["POSTGRES_DB"] == "smackend_private_notes"
    assert ".env" in (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    for key in ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD"):
        assert not example[key]
        assert len(env[key]) >= 32
        assert env[key] not in output
    if os.name != "nt":
        assert stat.S_IMODE((project / ".env").stat().st_mode) == 0o600
    second = scaffold.create_project(tmp_path, "another-app")
    assert env["DJANGO_SECRET_KEY"] != env_values(second / ".env")["DJANGO_SECRET_KEY"]
    assert env["POSTGRES_PASSWORD"] != env_values(second / ".env")["POSTGRES_PASSWORD"]


@pytest.mark.parametrize("name", ["", "MyApp", "bad name", "../escape", "nested/app", "c:\\outside",
                                 "con", "com1", "trailing-", "_leading", "a" * 49])
def test_invalid_names_leave_no_files(tmp_path, monkeypatch, capsys, name):
    monkeypatch.chdir(tmp_path)
    assert main(["init", name]) == 1
    assert "Choose a name" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("directory", [True, False])
def test_existing_destination_is_preserved(tmp_path, monkeypatch, capsys, directory):
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / "existing"
    if directory:
        destination.mkdir()
        content = destination / "keep.txt"
    else:
        content = destination
    content.write_text("keep my work", encoding="utf-8")
    assert main(["init", "existing"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert content.read_text(encoding="utf-8") == "keep my work"
    if directory:
        assert list(destination.iterdir()) == [content]


def test_failed_write_cleans_up_only_the_new_project(tmp_path, monkeypatch):
    keep = tmp_path / "keep.txt"
    keep.write_text("existing", encoding="utf-8")
    write = scaffold.write_file

    def fail_on_env(path: Path, *args, **kwargs):
        if path.name == ".env":
            raise OSError("simulated write failure")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(scaffold, "write_file", fail_on_env)
    with pytest.raises(OSError, match="simulated write failure"):
        scaffold.create_project(tmp_path, "new-app")
    assert list(tmp_path.iterdir()) == [keep]


def test_second_init_keeps_generated_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "keep-credentials"]) == 0
    path = tmp_path / "keep-credentials" / ".env"
    original = path.read_bytes()
    assert main(["init", "keep-credentials"]) == 1
    assert path.read_bytes() == original


def test_version_uses_the_installed_distribution(capsys):
    with pytest.raises(SystemExit) as result:
        main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == f"smackend {version('smackend')}"


def test_verify_requires_a_project_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["verify"]) == 2
    assert "manage.py" in capsys.readouterr().err
