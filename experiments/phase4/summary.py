"""Summarize benchmark runs from raw, credential-free artifacts."""

import argparse
import gzip
import json
from pathlib import Path
from statistics import median

from load import percentiles


def read_samples(path):
    with gzip.open(path, "rt", encoding="utf-8") as file:
        return [json.loads(line) for line in file]


def resource_summary(path):
    samples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    output = {"connection_peak": max((sum(row["connections"].values()) for row in samples), default=0)}
    for name in ("app", "postgres", "driver", "provider"):
        populations = {}
        rss = []
        threads = []
        for row in samples:
            processes = row["processes"].get(name)
            if not processes:
                continue
            rss.append((row["elapsed"], sum(value["rss"] for value in processes) / 2**20))
            threads.append(sum(value["threads"] for value in processes))
            for process in processes:
                values = populations.setdefault(process["pid"], [])
                values.append(process["cpu_s"])
        if not rss:
            continue
        warm = [value for timestamp, value in rss if 300 <= timestamp < 600]
        final = [value for timestamp, value in rss if timestamp >= 1500]
        output[name] = {
            "peak_rss_mib": round(max(value for _, value in rss), 2),
            "peak_threads": max(threads),
            "sampled_cpu_s": round(sum(values[-1] - values[0] for values in populations.values()), 2),
            "warm_median_rss_mib": round(median(warm), 2) if warm else None,
            "last_5min_median_rss_mib": round(median(final), 2) if final else None,
        }
    return output


def results(root):
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        for entry in sorted(folder.glob("*.jsonl.gz")):
            stem = entry.name.removesuffix(".jsonl.gz")
            summary = folder / f"{stem}.json"
            if not summary.is_file() or not summary.stat().st_size:
                continue
            sample = json.loads(summary.read_text(encoding="utf-8-sig"))
            timings = read_samples(entry)
            overall = percentiles([row["ms"] for row in timings])
            resources = resource_summary(folder / f"{stem}-resources.jsonl")
            yield {"trial": folder.name + "/" + stem, **sample, "overall_ms": overall,
                   "resources": resources, "observed_body_mismatches": sum(
                       bool(row["status"] in {101, 200, 201, 204} and not row["valid"]) for row in timings)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--prefix", default="")
    args = parser.parse_args()
    runs = [run for run in results(args.evidence) if run["trial"].startswith(args.prefix)]
    if args.json:
        print(json.dumps(runs, indent=2))
        return
    print("| Trial | Completed/s | Invalid | Missed/offered | Total p50/p95/p99 ms | "
          "Read p95 | Write p95 | Aggregate p95 | App RSS MiB / CPU s / threads | DB client peak |")
    print("|---|---:|---:|---:|---|---:|---:|---:|---|---:|")
    for run in runs:
        latency = "/".join(f"{value:.1f}" for value in run["overall_ms"].values())
        group = run["groups"]
        p95s = " | ".join(str(group.get(name, {}).get("latency_ms", {}).get("p95", "–"))
                          for name in ("read", "write", "aggregate"))
        app = run["resources"].get("app", {})
        app_values = " / ".join(str(app.get(name, "–")) for name in ("peak_rss_mib", "sampled_cpu_s", "peak_threads"))
        print(f"| {run['trial']} | {run['completed_rps']:.1f} | {run['completed'] - run['valid']} | "
              f"{run['late_or_busy']}/{run['offered']} | {latency} | {p95s} | {app_values} | "
              f"{run['resources']['connection_peak']} |")


if __name__ == "__main__":
    main()
