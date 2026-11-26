"""Boot every distinct auto-enabled profile from the installed CLI."""

from itertools import combinations
import json
import os
from pathlib import Path
import subprocess
import sys

from smackend.modules import MODULES


PROBE = """
import os
import sys
from django.core.asgi import get_asgi_application
from django.core.checks import ERROR, run_checks
from django.conf import settings

os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
get_asgi_application()
import config.asgi

configured = {name for name in sys.argv[1].split(',') if name}
active = {name for name in ('auth', 'storage', 'admin', 'http', 'ai', 'realtime')
          if getattr(settings, 'SMACKEND_' + name.upper() + '_ENABLED')}
assert configured == active, (configured, active)
assert not [issue for issue in run_checks() if issue.level >= ERROR]
from config.api import api
api.get_openapi_schema()
"""


def main(folder):
    names = tuple(module.name for module in MODULES)
    commands = set()
    for size in range(len(names) + 1):
        commands.update(combinations(names, size))
    folder.mkdir(parents=True, exist_ok=False)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("DJANGO_", "SMACKEND_", "POSTGRES_")) and key != "PYTHONPATH"}
    opened = set()
    for index, selection in enumerate(sorted(commands)):
        cli = "from smackend.cli import main; raise SystemExit(main())"
        name = "module-" + str(index)
        subprocess.run([sys.executable, "-c", cli, "init", name, *("--" + entry for entry in selection)],
                       cwd=folder, env=environment, check=True, capture_output=True, timeout=30)
        application = folder / name
        flags = set()
        for line in (application / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("SMACKEND_") and line.endswith("_ENABLED=true"):
                flags.add(line.removeprefix("SMACKEND_").split("_ENABLED=")[0].lower())
        if frozenset(flags) not in opened:
            subprocess.run([sys.executable, "-c", PROBE, ",".join(sorted(flags))],
                           cwd=application, env=environment, check=True, timeout=30)
            opened.add(frozenset(flags))
    print(json.dumps({"flag_subsets_generated": len(commands), "distinct_profiles_booted": len(opened),
                      "profiles": sorted(sorted(profile) for profile in opened)}, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
