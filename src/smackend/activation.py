"""Activate existing project modules without regenerating application files."""

from contextlib import contextmanager
from importlib.metadata import distribution, version
from io import StringIO
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile
import tomllib
from urllib.parse import urlsplit
from urllib.request import url2pathname

from dotenv.parser import parse_stream

from .modules import MODULES, scaffold_modules


FLAGS = {module.setting: module.name for module in MODULES}
TRUE = {"true", "1"}
FALSE = {"false", "0"}
REQUIREMENT = re.compile(r"smackend\[([a-z0-9_, -]+)\]\s*==\s*(0\.1\.0a(?:9|10))", re.IGNORECASE)


def read_text(path):
    return path.read_bytes().decode("utf-8")


def project_details(root):
    if not all((root / name).is_file() for name in ("manage.py", "pyproject.toml", ".env", ".env.example")):
        raise ValueError("Run this inside a generated Smackend project with both .env files and manage.py.")
    source = read_text(root / "pyproject.toml")
    document = tomllib.loads(source)
    project = document.get("project", {})
    if not isinstance(project, dict) or not isinstance(project.get("dependencies"), list):
        raise ValueError("Expected a generated [project] dependency list.")
    requirements = [item for item in project["dependencies"]
                    if isinstance(item, str) and re.match(r"(?i)^smackend(?![a-z0-9_.-])", item)]
    if not project.get("name") or len(requirements) != 1 or not REQUIREMENT.fullmatch(requirements[0]):
        raise ValueError("Expected one Smackend dependency pinned to a9 or a10 in [project].dependencies.")
    return source, document, requirements[0]


def update_requirement(source, document, old, new):
    if old == new:
        return source
    # Generated projects use a single quoted TOML literal. Verify the parsed
    # result so a comment or similarly named value cannot be changed instead.
    pattern = re.compile(r"(?P<q>['\"])" + re.escape(old) + r"(?P=q)")
    expected = document["project"]["dependencies"]
    expected[expected.index(old)] = new
    for match in pattern.finditer(source):
        candidate = source[:match.start()] + match[0].replace(old, new, 1) + source[match.end():]
        try:
            if tomllib.loads(candidate) == document:
                return candidate
        except tomllib.TOMLDecodeError:
            continue
    raise ValueError("Could not update the Smackend pin safely. Use a single quoted dependency literal.")


def flag_text(path, selected=()):
    source = read_text(path)
    bom = "\ufeff" if source.startswith("\ufeff") else ""
    values = {}
    parts = [bom]
    for record in parse_stream(StringIO(source.removeprefix(bom))):
        text = record.original.string
        if record.error or (record.key is not None and record.value is None):
            raise ValueError(f"Invalid assignment in {path.name} at line {record.original.line}.")
        if record.key in FLAGS:
            if record.key in values:
                raise ValueError(f"Duplicate {record.key} in {path.name}.")
            if record.value not in TRUE | FALSE:
                raise ValueError(f"Set {record.key} in {path.name} to true, false, 1 or 0.")
            values[record.key] = record.value
            if FLAGS[record.key] in selected and record.value not in TRUE:
                pattern = r"(?m)^(\s*(?:export[ \t]+)?['\"]?" + record.key + r"['\"]?[ \t]*=[ \t]*)(?:'[^'\r\n]*'|\"[^\"\r\n]*\"|[^#\r\n]*?)(?=[ \t]*(?:#|\r?$))"
                text, count = re.subn(pattern, r"\g<1>true", text, count=1)
                if count != 1:
                    raise ValueError(f"Use a single-line assignment for {record.key} in {path.name}.")
        parts.append(text)
    output = "".join(parts)
    missing = [key for key in FLAGS if FLAGS[key] in selected and key not in values]
    if missing:
        newline = "\r\n" if "\r\n" in source else "\n"
        if output and not output.endswith("\n"):
            output += newline
        output += "".join(f"{key}=true{newline}" for key in missing)
    return values, output


def effective_flags(values):
    flags = {key: os.environ.get(key, values.get(key, "false")) for key in FLAGS}
    for key, value in flags.items():
        if value not in TRUE | FALSE:
            raise ValueError(f"Set {key} to true, false, 1 or 0.")
    return {FLAGS[key] for key, value in flags.items() if value in TRUE}


def python_path(root):
    environment = root / os.environ.get("UV_PROJECT_ENVIRONMENT", ".venv")
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run_local(args, root, timeout=180):
    return subprocess.run(args, cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})


def probe(root, action, expected=()):
    python = python_path(root)
    if not python.is_file():
        return {"error": "Project interpreter is missing. Run uv sync or smackend add first."}
    result = run_local([str(python), "-m", "smackend.activation_checks", action, *sorted(expected)], root)
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("SMACKEND_ACTIVATION "):
            return json.loads(line.removeprefix("SMACKEND_ACTIVATION "))
    # The a9 wheel has no activation probe. Use its existing read-only verify
    # entry point to check migration state and query metadata separately.
    if action == "status":
        metadata = run_local([str(python), "-c",
                              "from importlib.metadata import version; print(version('smackend'))"], root)
        if metadata.returncode == 0 and metadata.stdout.strip() == "0.1.0a9":
            connection = run_local([str(python), "-m", "smackend.verify", "connection"], root)
            migrations = run_local([str(python), "-m", "smackend.verify", "migrations"], root) if connection.returncode == 0 else None
            return {"version": "0.1.0a9", "pending": None if migrations is None else bool(migrations.returncode),
                    "offline": migrations is None}
    return {"error": "Could not run project verification. Check the project interpreter and run manage.py check."}


def wheel_links(root, target, explicit):
    candidates = [folder for parent in (root, *root.parents) for folder in (parent / "dist", parent / "wheelhouse")]
    try:
        direct = json.loads(distribution("smackend").read_text("direct_url.json") or "{}")
        url = urlsplit(direct.get("url", ""))
        if url.scheme == "file":
            candidates.insert(0, Path(url2pathname(url.path)).parent)
    except (OSError, ValueError):
        pass
    links = list(explicit)
    for directory in candidates:
        if directory.is_dir() and any(directory.glob(f"smackend-{target}-*.whl")) and str(directory) not in links:
            links.append(str(directory))
    return [option for link in links for option in ("--find-links", link)]


def atomic_write(path, data, mode):
    if path.is_symlink():
        raise ValueError(f"Use an ordinary local file for {path.name}.")
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".smackend-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def project_lock(root):
    lock = root / ".smackend-activation.lock"
    try:
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
    except FileExistsError:
        raise ValueError("An activation lock exists. Check for an active add process before removing .smackend-activation.lock.") from None
    try:
        yield
    finally:
        lock.unlink()


def add(root, args):
    if args.dry_run:
        return activate(root, args)
    with project_lock(root):
        return activate(root, args)


def activate(root, args):
    source, document, old = project_details(root)
    match = REQUIREMENT.fullmatch(old)
    assert match
    extras = list(dict.fromkeys(item.strip().lower() for item in match[1].split(",")))
    pinned = match[2]
    target = version("smackend") if args.upgrade else pinned
    if pinned != target and (pinned, target) != ("0.1.0a9", "0.1.0a10"):
        raise ValueError("Only the explicit a9-to-a10 upgrade is supported.")
    if pinned != version("smackend") and not args.upgrade:
        raise ValueError("The project pins a9. Preview with smackend add --upgrade --dry-run, then use --upgrade.")
    values, _ = flag_text(root / ".env")
    enabled = effective_flags(values)
    selected = scaffold_modules(enabled | set(args.modules))
    for key, name in FLAGS.items():
        if name in selected and os.environ.get(key) in FALSE:
            raise ValueError(f"Remove the process-level {key}=false override before enabling this module.")
    needed = ["backend", *(module.extra for module in MODULES if module.name in selected and module.extra)]
    missing = [extra for extra in needed if extra not in extras]
    extras_text = ",".join([*extras, *missing])
    updated = old if not missing and pinned == target else f"smackend[{extras_text}]=={target}"
    planned = {"pyproject.toml": update_requirement(source, document, old, updated),
               **{name: flag_text(root / name, selected)[1] for name in (".env", ".env.example")}}
    changes = {name: data for name, data in planned.items() if read_text(root / name) != data}
    print(f"{document['project']['name']} | {updated}", flush=True)
    print("Enabled after activation: " + (", ".join(sorted(selected)) or "none"), flush=True)
    print("Files to update: " + (", ".join(changes) if changes else "none"), flush=True)
    if args.dry_run:
        print("Dry run. uv sync, checks and migration planning would follow.")
        return 0

    uv = shutil.which("uv")
    if not uv:
        raise ValueError("Install uv and make it available on PATH.")
    links = wheel_links(root, target, args.find_links)
    names = (*changes, "uv.lock")
    if any((root / name).is_symlink() for name in names):
        raise ValueError("Activation requires ordinary local project files.")
    backups = {name: ((root / name).read_bytes(), stat.S_IMODE((root / name).stat().st_mode))
               if (root / name).exists() else None for name in names}
    had_python = python_path(root).is_file()
    attempted_sync = False
    try:
        for name, content in changes.items():
            atomic_write(root / name, content.encode("utf-8"), backups[name][1])
        attempted_sync = True
        sync = run_local([uv, "sync", "--project", str(root), "--inexact", *links], root, timeout=300)
        if sync.returncode:
            raise ValueError("uv sync failed. Check wheel availability (--find-links) and dependency constraints.")
        checked = probe(root, "check", selected)
        if checked.get("error") or checked.get("version") != target:
            raise ValueError(checked.get("error") or "The synced interpreter did not install the requested Smackend version.")
    except (Exception, KeyboardInterrupt):
        for name, original in backups.items():
            if original is None:
                (root / name).unlink(missing_ok=True)
            elif not (root / name).exists() or (root / name).read_bytes() != original[0]:
                atomic_write(root / name, *original)
        print("Restored project files and lockfile.", file=sys.stderr)
        if attempted_sync and had_python:
            try:
                locked = ["--locked"] if backups["uv.lock"] is not None else []
                restore = run_local([uv, "sync", "--project", str(root), "--inexact", *locked, *wheel_links(root, pinned, args.find_links)],
                                    root, timeout=90)
                if restore.returncode:
                    print("Project environment needs repair. Run uv sync with the original wheel directory.", file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired):
                print("Could not restore the project environment. Run uv sync with the original wheel directory.", file=sys.stderr)
            if backups["uv.lock"] is None:
                (root / "uv.lock").unlink(missing_ok=True)
        elif attempted_sync:
            print("A new project environment may remain. Run uv sync before using it.", file=sys.stderr)
        raise

    print("Dependencies installed and Django checks passed.", flush=True)
    print("Activation saved. Database changes, once applied, are kept.", flush=True)
    result = probe(root, "migrate", selected)
    if result.get("error"):
        print(f"Migration verification failed: {result['error']}", file=sys.stderr)
        print("Activation is saved. Review migrate --plan, then rerun smackend add or verify.", file=sys.stderr)
        return 1
    if result.get("offline"):
        print("Migrations and database verification are pending. Check connectivity and rerun smackend add.")
        return 0
    if result.get("custom"):
        print("Pending application migrations require review. Run uv run python manage.py migrate --plan.")
        return 0
    print("Migration and model drift checks passed. Run uv run smackend verify for the full suite.")
    return 0


def status(root):
    _, document, requirement = project_details(root)
    match = REQUIREMENT.fullmatch(requirement)
    assert match
    values, _ = flag_text(root / ".env")
    example, _ = flag_text(root / ".env.example")
    enabled = effective_flags(values)
    result = probe(root, "status")
    print(f"Project: {document['project']['name']}")
    print(f"Smackend: installed {result.get('version', 'unknown')}, pinned {match[2]}, CLI {version('smackend')}")
    extras = {item.strip().lower() for item in match[1].split(",")}
    for module in MODULES:
        active = module.name in enabled
        notes = []
        if active:
            missing = sorted(set(module.requires) - enabled)
            if missing:
                notes.append("requires " + ", ".join(missing))
            if module.extra and module.extra not in extras:
                notes.append("missing extra " + module.extra)
        if (values.get(module.setting) in TRUE) != (example.get(module.setting) in TRUE):
            notes.append(".env and .env.example differ")
        if module.setting in os.environ:
            notes.append("process environment overrides .env")
        print(f"  {module.name}: {'enabled' if active else 'disabled'}" + (f" ({'; '.join(notes)})" if notes else ""))
    if result.get("error"):
        print(result["error"])
    elif result.get("offline"):
        print("Migrations: unknown (database connection unavailable).")
    else:
        print("Migrations: " + ("pending" if result.get("pending") else "up to date"))
    if "enabled" in result and enabled != set(result["enabled"]):
        print("Effective Django settings differ from the environment flags.")
    return int(bool(result.get("error")))
