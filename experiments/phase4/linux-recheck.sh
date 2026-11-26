#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
scratch="$repo/.local/phase4-linux"
runner="$repo/experiments/phase4/linux-local.sh"
completed="$scratch/followup.status"
trap 'code=$?; printf "%s\n" "$code" > "$scratch/recheck.status"' EXIT
if [[ ${1:-} == wait-for-followup ]]; then
  for ((check=0; check<150; check++)); do
    if [[ -s "$completed" ]]; then break; fi
    sleep 10
  done
  if [[ ! -s "$completed" || $(cat "$completed") != 0 ]]; then
    echo "Complete the preceding comparisons before running this recheck." >&2
    exit 1
  fi
fi
runtime=$(cat "$scratch/runtime-dir")
python="$runtime/venv/bin/python"
export UV_CACHE_DIR="$runtime/uv-cache" UV_LINK_MODE=copy
export SMACKEND_QUAL_PYTHON="$python" SMACKEND_QUAL_SCRATCH="${SMACKEND_QUAL_SCRATCH:-$(mktemp -d -t smackend-crossover.XXXXXXXX)}"
export SMACKEND_QUAL_PG_PORT=55441

"$scratch/uv" pip install --offline --reinstall-package smackend --python "$python" \
  "$repo/dist/smackend-0.1.0a7-py3-none-any.whl[storage]"
bash "$runner" linux-a7-confirm --profile --matrix mixed:100:20:50 mixed:100:20:50

sealed="${SMACKEND_QUAL_FINAL_WHEEL:-$repo/dist/smackend-0.1.0a8-py3-none-any.whl}"
"$scratch/uv" pip install --offline --reinstall-package smackend --python "$python" "$sealed[storage]"
bash "$runner" linux-secure-confirm --profile --auth-ip-budget 400 --matrix \
  mixed:100:20:50 mixed:100:20:50 mixed-realtime:150:35:100
