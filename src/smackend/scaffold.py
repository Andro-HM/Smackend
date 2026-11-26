"""Write a standalone project from the files installed with Smackend."""

from contextlib import ExitStack, suppress
from importlib.metadata import version
from importlib.resources import files
from importlib.resources.abc import Traversable
import os
from pathlib import Path
import re
from secrets import token_urlsafe

from .modules import MODULES, scaffold_modules

FILE_NAMES = {"gitignore.txt": ".gitignore", "env.example": ".env.example"}
WINDOWS_RESERVED = {"aux", "con", "nul", "prn"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def validate_name(name: str) -> None:
    if not re.fullmatch(r"[a-z](?:[a-z0-9_-]{0,46}[a-z0-9])?", name) or name in WINDOWS_RESERVED:
        raise ValueError(
            "Choose a name of up to 48 characters that starts with a lowercase ASCII letter "
            "and ends with a letter or digit. Hyphens and underscores are allowed inside the name. "
            "Device names are reserved."
        )


def remove_file(path: Path) -> None:
    with suppress(OSError):
        path.unlink()


def remove_directory(path: Path) -> None:
    with suppress(OSError):
        path.rmdir()


def write_file(path: Path, content: str, rollback: ExitStack, *, private: bool = False) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644)
    rollback.callback(remove_file, path)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(content)


def copy_template(source: Traversable, target: Path, replacements: dict[str, str], rollback: ExitStack) -> None:
    target.mkdir()
    rollback.callback(remove_directory, target)
    for item in sorted(source.iterdir(), key=lambda entry: entry.name):
        if item.name == "__pycache__":
            continue
        destination = target / FILE_NAMES.get(item.name, item.name)
        if item.is_dir():
            copy_template(item, destination, replacements, rollback)
        else:
            content = item.read_text(encoding="utf-8")
            for marker, value in replacements.items():
                content = content.replace(marker, value)
            write_file(destination, content, rollback)


def create_project(parent: Path, name: str, *, auth=False, storage=False, admin=False, http=False, ai=False,
                   realtime=False) -> Path:
    validate_name(name)
    target = parent / name
    requested = {"auth": auth, "storage": storage, "admin": admin, "http": http, "ai": ai, "realtime": realtime}
    enabled = scaffold_modules(name for name, selected in requested.items() if selected)
    extras = ["backend", *(module.extra for module in MODULES if module.extra and module.name in enabled)]
    replacements = {
        "__PROJECT_NAME__": name,
        "__DATABASE_NAME__": "smackend_" + name.replace("-", "_"),
        "__SMACKEND_VERSION__": version("smackend"),
        "__SMACKEND_EXTRAS__": ",".join(extras),
        **{f"__{module.setting}__": "true" if module.name in enabled else "false" for module in MODULES},
    }
    try:
        with ExitStack() as rollback:
            copy_template(files("smackend").joinpath("template"), target, replacements, rollback)
            env = (target / ".env.example").read_text(encoding="utf-8")
            env = env.replace("DJANGO_SECRET_KEY=\n", f"DJANGO_SECRET_KEY={token_urlsafe(48)}\n")
            env = env.replace("POSTGRES_PASSWORD=\n", f"POSTGRES_PASSWORD={token_urlsafe(32)}\n")
            write_file(target / ".env", env, rollback, private=True)
            rollback.pop_all()
    except FileExistsError as exc:
        if exc.filename == os.fspath(target):
            raise FileExistsError(f"Destination already exists: {target}") from exc
        raise
    return target
