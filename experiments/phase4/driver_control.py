"""Use native curl as an independent client against the no-DB simulator."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

from load import percentiles
from run import process, ready


def main():
    port = "8099"
    url = f"http://127.0.0.1:{port}/"
    count = 1500
    config = f'url = "{url}"\noutput = "{os.devnull}"\n' * count
    cmd = ["curl", "--parallel", "--parallel-immediate", "--parallel-max", "32", "--silent", "--show-error",
           "--max-time", "15", "--noproxy", "*", "--write-out", "%{http_code} %{time_total}\n", "--config", "-"]
    folder = Path(__file__).resolve().parent
    with TemporaryDirectory() as scratch, process(
        [sys.executable, "-m", "uvicorn", "provider:application", "--host", "127.0.0.1",
         "--port", port, "--no-access-log", "--lifespan", "off"],
        folder=folder, env=os.environ, log=Path(scratch) / "provider.log",
    ) as server:
        ready(server, url)
        began = monotonic()
        result = subprocess.run(cmd, text=True, input=config, capture_output=True, check=True, timeout=45)
        elapsed = monotonic() - began
        status, latency = zip(*(row.split() for row in result.stdout.splitlines()))
        print(json.dumps({"client": "curl parallel", "offered": count, "completed": len(status),
                          "successes": status.count("200"), "wall_s": elapsed, "completed_rps": len(status) / elapsed,
                          "latency_ms": percentiles([float(value) * 1000 for value in latency])}, indent=2))


if __name__ == "__main__":
    main()
