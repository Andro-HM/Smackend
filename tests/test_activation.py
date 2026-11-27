from importlib.metadata import version
import tomllib

import pytest
from django.core.exceptions import ImproperlyConfigured

from smackend.cli import main
from smackend.modules import MODULES, configure_settings


def test_init_modules_selects_dependencies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "combined", "--admin", "--storage", "--ai", "--realtime"]) == 0
    root = tmp_path / "combined"
    env = (root / ".env").read_text(encoding="utf-8")
    for module in MODULES:
        assert f"{module.setting}=true" in env
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert f"smackend[backend,auth,storage,http,ai,realtime]=={version('smackend')}" in project["dependencies"]


@pytest.mark.parametrize("name", ["STORAGE", "ADMIN"])
def test_disabled_dependency_has_actionable_error(name):
    environment = {f"SMACKEND_{name}_ENABLED": True}
    with pytest.raises(ImproperlyConfigured, match="requires SMACKEND_AUTH_ENABLED"):
        configure_settings({}, environment, boolean=lambda source, key: source.get(key, False), production=False)


@pytest.fixture
def existing_project(tmp_path, monkeypatch):
    from smackend import activation

    monkeypatch.chdir(tmp_path)
    assert main(["init", "existing"]) == 0
    root = tmp_path / "existing"
    monkeypatch.chdir(root)
    monkeypatch.setattr(activation, "version", lambda _: "0.1.0a10")
    config = root / "pyproject.toml"
    config.write_text(config.read_text().replace(version("smackend"), "0.1.0a10"), encoding="utf-8")
    for module in MODULES:
        monkeypatch.delenv(module.setting, raising=False)
    monkeypatch.setattr(activation.shutil, "which", lambda name: name)
    return root


def stub_activation(monkeypatch, *, install=0, check=None, migration=None):
    from types import SimpleNamespace
    from smackend import activation

    def sync(args, root, **kwargs):
        (root / "uv.lock").write_text("updated", encoding="utf-8")
        return SimpleNamespace(returncode=install)

    def probe(root, action, expected=()):
        if action == "check":
            return {"version": "0.1.0a10"} if check is None else check
        return {} if migration is None else migration

    monkeypatch.setattr(activation, "run_local", sync)
    monkeypatch.setattr(activation, "probe", probe)


@pytest.mark.parametrize(("module", "enabled"), [
    ("auth", {"auth"}), ("storage", {"auth", "storage"}), ("admin", {"auth", "admin"}),
    ("http", {"http"}), ("ai", {"auth", "http", "ai"}), ("realtime", {"auth", "realtime"}),
])
def test_add_each_module(existing_project, monkeypatch, module, enabled):
    stub_activation(monkeypatch)
    assert main(["add", module]) == 0
    for filename in (".env", ".env.example"):
        values = dict(line.split("=", 1) for line in (existing_project / filename).read_text().splitlines()
                      if line and not line.startswith("#") and "=" in line)
        assert {m.name for m in MODULES if values[m.setting] == "true"} == enabled
    requirement = tomllib.loads((existing_project / "pyproject.toml").read_text())["project"]["dependencies"][0]
    assert set(requirement.split("[", 1)[1].split("]", 1)[0].split(",")) == {
        "backend", *(m.extra for m in MODULES if m.name in enabled and m.extra),
    }


def test_multi_add_preserves_custom_project_and_is_idempotent(existing_project, monkeypatch):
    stub_activation(monkeypatch)
    dotenv = existing_project / ".env"
    original = dotenv.read_bytes()
    dotenv.write_bytes(original + b'\nCUSTOM_TOKEN="multiline\nSMACKEND_ADMIN_ENABLED=false\nend"\n')
    metadata = existing_project / "pyproject.toml"
    metadata.write_text(metadata.read_text().replace('"django==5.2.19",', '"django==5.2.19", # application note') +
                        "\n[tool.application]\npolicy = 'custom'\n", encoding="utf-8")
    settings = existing_project / "config" / "settings.py"
    settings.write_bytes(settings.read_bytes() + b"\nCUSTOM = True\n")
    settings_before = settings.read_bytes()
    assert main(["add", "ai", "storage"]) == 0
    assert b'CUSTOM_TOKEN="multiline\nSMACKEND_ADMIN_ENABLED=false\nend"' in dotenv.read_bytes()
    assert b"# application note" in metadata.read_bytes()
    assert tomllib.loads(metadata.read_text())["tool"]["application"]["policy"] == "custom"
    assert settings.read_bytes() == settings_before
    for line in original.splitlines():
        if line.startswith((b"DJANGO_SECRET_KEY=", b"POSTGRES_PASSWORD=")):
            assert line in dotenv.read_bytes()
    snapshot = {filename: (existing_project / filename).read_bytes()
                for filename in ("pyproject.toml", ".env", ".env.example", "uv.lock")}
    assert main(["add", "storage", "ai"]) == 0
    assert all((existing_project / name).read_bytes() == data for name, data in snapshot.items())


def test_a9_explicit_upgrade_and_dry_run(existing_project, monkeypatch, capsys):
    from smackend import activation

    metadata = existing_project / "pyproject.toml"
    metadata.write_text(metadata.read_text().replace("0.1.0a10", "0.1.0a9"), encoding="utf-8")
    before = metadata.read_bytes()
    assert main(["add", "auth"]) == 1
    assert "--upgrade" in capsys.readouterr().err
    monkeypatch.setattr(activation, "run_local", lambda *args: pytest.fail("dry run called uv"))
    assert main(["add", "ai", "--upgrade", "--dry-run"]) == 0
    assert "ai, auth, http" in capsys.readouterr().out
    assert metadata.read_bytes() == before
    assert not (existing_project / "uv.lock").exists()
    assert not (existing_project / ".smackend-activation.lock").exists()


@pytest.mark.parametrize("failure", ("install", "configuration"))
def test_preflight_restores_every_file(existing_project, monkeypatch, capsys, failure):
    stub_activation(monkeypatch, install=(failure == "install"),
                    check={"error": "Django configuration failed"} if failure == "configuration" else None)
    (existing_project / "uv.lock").write_bytes(b"original")
    before = {name: (existing_project / name).read_bytes()
              for name in (".env", ".env.example", "pyproject.toml", "uv.lock")}
    assert main(["add", "ai"]) == 1
    assert "Restored project files" in capsys.readouterr().err
    assert all((existing_project / name).read_bytes() == old for name, old in before.items())
    assert not (existing_project / ".smackend-activation.lock").exists()


@pytest.mark.parametrize(("outcome", "code"), [({"offline": True}, 0), ({"custom": True}, 0),
                                              ({"error": "migration failed"}, 1)])
def test_pending_migrations_keep_activation(existing_project, monkeypatch, outcome, code):
    stub_activation(monkeypatch, migration=outcome)
    assert main(["add", "storage"]) == code
    assert "SMACKEND_STORAGE_ENABLED=true" in (existing_project / ".env").read_text()


def test_status_and_process_override(existing_project, monkeypatch, capsys):
    from smackend import activation

    monkeypatch.setenv("SMACKEND_AI_ENABLED", "true")
    monkeypatch.setenv("SMACKEND_AUTH_ENABLED", "false")
    monkeypatch.setattr(activation, "probe", lambda *args: {"version": "0.1.0a10", "pending": True})
    assert main(["status"]) == 0
    output = capsys.readouterr().out
    assert "requires auth, http" in output and "missing extra ai" in output and "Migrations: pending" in output
    assert main(["add", "ai"]) == 1
    assert "process-level" in capsys.readouterr().err


def test_invalid_module_and_active_lock(existing_project):
    with pytest.raises(SystemExit) as error:
        main(["add", "unknown"])
    assert error.value.code == 2
    lock = existing_project / ".smackend-activation.lock"
    lock.touch()
    assert main(["add", "auth"]) == 1
    assert lock.exists()


def test_already_enabled_keeps_existing_extra_order(existing_project, monkeypatch):
    stub_activation(monkeypatch)
    metadata = existing_project / "pyproject.toml"
    metadata.write_text(metadata.read_text().replace("smackend[backend]", "smackend[backend,auth,s3,storage]"),
                        encoding="utf-8")
    for name in (".env", ".env.example"):
        p = existing_project / name
        p.write_text(p.read_text().replace("SMACKEND_AUTH_ENABLED=false", "SMACKEND_AUTH_ENABLED=true")
                     .replace("SMACKEND_STORAGE_ENABLED=false", "SMACKEND_STORAGE_ENABLED=true"), encoding="utf-8")
    before = metadata.read_bytes()
    assert main(["add", "storage"]) == 0
    assert metadata.read_bytes() == before
