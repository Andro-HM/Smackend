"""Use native curl as an independent client against the no-DB simulator."""

import argparse
import asyncio
import json

import httpx
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic

from load import percentiles
from run import process, ready


async def python_control(url, count, concurrency):
    timings = []

    async with httpx.AsyncClient(trust_env=False, limits=httpx.Limits(
        max_connections=concurrency, max_keepalive_connections=concurrency,
    )) as client:
        async def worker(size):
            for _ in range(size):
                start = monotonic()
                response = await client.get(url)
                timings.append((response.status_code, (monotonic() - start) * 1000))

        start = monotonic()
        await asyncio.gather(*(worker(count // concurrency + (index < count % concurrency))
                               for index in range(concurrency)))
    return {"client": "HTTPX", "completed": len(timings),
            "successes": sum(status == 200 for status, _ in timings),
            "wall_s": monotonic() - start, "latency_ms": percentiles([ms for _, ms in timings])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="8099")
    port = parser.parse_args().port
    url = f"http://127.0.0.1:{port}/"
    count = 1500
    config = f'url = "{url}"\noutput = "{os.devnull}"\n' * count
    cmd = [("curl.exe" if os.name == "nt" else "curl"), "--parallel", "--parallel-immediate", "--parallel-max", "32", "--silent", "--show-error",
           "--max-time", "15", "--noproxy", "*", "--write-out", "%{http_code} %{time_total}\n", "--config", "-"]
    folder = Path(__file__).resolve().parent
    with process(
        [sys.executable, "-m", "uvicorn", "provider:application", "--host", "127.0.0.1",
         "--port", port, "--no-access-log", "--lifespan", "off"],
        folder=folder, env=os.environ, log=None,
    ) as server:
        ready(server, url)
        began = monotonic()
        result = subprocess.run(cmd, text=True, input=config, capture_output=True, check=True, timeout=45)
        elapsed = monotonic() - began
        python_result = asyncio.run(python_control(url, count, 32))
        status, latency = zip(*(row.split() for row in result.stdout.splitlines()))
        print(json.dumps({"client": "curl parallel", "offered": count, "completed": len(status),
                          "successes": status.count("200"), "wall_s": elapsed, "completed_rps": len(status) / elapsed,
                          "latency_ms": percentiles([float(value) * 1000 for value in latency]),
                          "matched_burst": python_result}, indent=2))


if __name__ == "__main__":
    main()
