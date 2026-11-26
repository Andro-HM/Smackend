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
