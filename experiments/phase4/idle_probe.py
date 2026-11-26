"""Compare startup and idle resource use with modules disabled and enabled."""

import json
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
from time import sleep

from rehearsal import database, run
from run import process, ready, record_resources
from summary import resource_summary


CLI = "from smackend.cli import main; raise SystemExit(main())"
DISABLED = """
import config.asgi, sys
from django.urls import get_resolver
get_resolver().url_patterns
modules = {"channels", "websockets", "jsonschema", "allauth", "httpx", "PIL", "storages"}
assert not modules.intersection(sys.modules), modules.intersection(sys.modules)
"""


def measure(folder, name, options, port):
    with database() as env:
        run(folder, env, "-c", CLI, "init", name, *options)
        project = folder / name
        run(project, env, "manage.py", "migrate", "--noinput")
        if not options:
            subprocess.run([sys.executable, "-c", DISABLED], cwd=project, env=env, check=True, timeout=20)
        argv = [sys.executable, "-m", "uvicorn", "config.asgi:application", "--host", "127.0.0.1",
                "--port", str(port), "--workers", "2", "--no-access-log", "--lifespan", "off"]
        with process(argv, folder=project, env=env, log=folder / f"{name}.log") as app:
            boot = ready(app, f"http://127.0.0.1:{port}/api/ready")
            destination = folder / f"{name}-idle.jsonl"
            stop = Event()
            observer = Thread(target=record_resources, args=(stop, destination), kwargs={
                "app": app.pid, "driver": None, "provider": None, "database": None,
                "dsn": env["SMACKEND_SMOKE_DSN"], "name": env["POSTGRES_DB"],
            })
            observer.start()
            sleep(10)
            stop.set()
            observer.join()
        return {"boot_ms": boot, **resource_summary(destination)}


if __name__ == "__main__":
    scratch = Path(sys.argv[1]).resolve()
    scratch.mkdir(parents=True, exist_ok=False)
    print(json.dumps({
        "disabled": measure(scratch, "core-only", (), 8104),
        "combined": measure(scratch, "all-modules", ("--ai", "--realtime", "--storage", "--admin"), 8104),
    }, indent=2))
