"""Command-line entry point."""

import argparse
from importlib.metadata import version
from pathlib import Path
import sys
import subprocess

from .scaffold import create_project
from .modules import MODULES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smackend", description="Create a Django and PostgreSQL project.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('smackend')}")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init = subcommands.add_parser("init", help="generate a project in a new directory")
    init.add_argument("project_name", help="a lowercase name such as private-notes")
    for module in MODULES:
        init.add_argument(f"--{module.name}", action="store_true", help=module.help)
    verify = subcommands.add_parser("verify", help="check and test the Django project in this directory")
    verify.add_argument("--settings", help="Django settings module (defaults to DJANGO_SETTINGS_MODULE or config.settings)")
    verify.add_argument("test_labels", nargs="*", help="optional native Django test labels")
    add = subcommands.add_parser("add", help="enable modules in an existing generated project")
    add.add_argument("modules", nargs="*", metavar="MODULE",
                     help=", ".join(module.name for module in MODULES))
    add.add_argument("--upgrade", action="store_true", help="explicitly upgrade an a9 project to this CLI's version")
    add.add_argument("--dry-run", action="store_true", help="preview changes without installing or writing")
    add.add_argument("--find-links", action="append", default=[], metavar="PATH", help="additional uv wheel location")
    subcommands.add_parser("status", help="show the current project's modules and migration state")
    args = parser.parse_args(argv)

    if args.command in {"add", "status"}:
        from .activation import add as add_modules, project_details, status

        try:
            root = Path.cwd()
            if args.command == "status":
                return status(root)
            unknown = set(args.modules) - {module.name for module in MODULES}
            if unknown:
                parser.error("unknown modules: " + ", ".join(sorted(unknown)))
            if not args.modules and not args.upgrade:
                parser.error("specify at least one module or --upgrade")
            project_details(root)
            return add_modules(root, args)
        except KeyboardInterrupt:
            print("smackend: interrupted. Inspect project status and migration state before retrying.", file=sys.stderr)
            return 130
        except subprocess.TimeoutExpired:
            print("smackend: a project command timed out. Inspect project status before retrying.", file=sys.stderr)
            return 1
        except (OSError, UnicodeError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else "Could not access project files or run the required tool."
            print(f"smackend: {message}", file=sys.stderr)
            return 1

    if args.command == "verify":
        from .verify import verify_project

        return verify_project(Path.cwd(), args.settings, args.test_labels)

    try:
        project = create_project(Path.cwd(), args.project_name,
                                 **{module.name: getattr(args, module.name) for module in MODULES})
    except (OSError, ValueError) as exc:
        print(f"smackend: {exc}", file=sys.stderr)
        return 1

    print(
        f"Created {project.name}.\n\n"
        f"  cd {project.name}\n"
        "  uv sync --python 3.11\n"
        "  docker compose up -d --wait postgres\n"
        "  uv run python manage.py migrate\n"
        "  uv run python manage.py runserver 127.0.0.1:8000\n\n"
        "Open http://127.0.0.1:8000/. See the generated README.md for details.\n"
        "For an unpublished wheel, add --find-links /path/to/dist to uv sync."
    )
    return 0
