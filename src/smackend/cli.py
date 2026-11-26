"""Command-line entry point."""

import argparse
from importlib.metadata import version
from pathlib import Path
import sys

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
    args = parser.parse_args(argv)

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
