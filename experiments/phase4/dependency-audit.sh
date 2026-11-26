#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
scratch="$repo/.local/phase4-linux"
"$scratch/venv/bin/python" -m pip_audit --no-deps --disable-pip --timeout 12 \
  -r "$scratch/candidate-audit-requirements.txt" --format json \
  --output "$repo/experiments/phase4/evidence/python-audit-a8-pins.json"
