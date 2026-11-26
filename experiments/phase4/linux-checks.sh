#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
scratch="$repo/.local/phase4-linux"
mode=${1:?Provide soak, capacity, or compare}
if [[ ! $mode =~ ^(soak|capacity|compare)$ ]]; then exit 2; fi
: > "$scratch/$mode.status"
trap 'code=$?; printf "%s\n" "$code" > "$scratch/$mode.status"' EXIT
runner="$repo/experiments/phase4/linux-local.sh"
runtime=$(cat "$scratch/runtime-dir")
python="$runtime/venv/bin/python"
if [[ ! -x "$python" ]]; then
  runtime=$(mktemp -d -t smackend-linux-runtime.XXXXXXXX)
  "$scratch/uv" venv --python /usr/bin/python3 "$runtime/venv"
  python="$runtime/venv/bin/python"
  printf '%s\n' "$runtime" > "$scratch/runtime-dir"
fi
export UV_CACHE_DIR="$runtime/uv-cache" UV_LINK_MODE=copy
export SMACKEND_QUAL_PYTHON="$python" SMACKEND_QUAL_SCRATCH="${SMACKEND_QUAL_SCRATCH:-$(mktemp -d -t smackend-check.XXXXXXXX)}"
export SMACKEND_QUAL_PG_PORT=55441
sealed="${SMACKEND_QUAL_FINAL_WHEEL:-$repo/dist/smackend-0.1.0a8-py3-none-any.whl}"

if [[ $mode == compare ]]; then
  comparator="${SMACKEND_QUAL_COMPARISON_WHEEL:-$repo/.local/phase4-middleware-control/smackend-0.1.0a8-py3-none-any.whl}"
  label=linux-middleware-a8
  if [[ ! -f "$comparator" ]]; then
    comparator="$sealed"
    label=linux-a8
  fi
  "$scratch/uv" pip install --reinstall-package smackend --python "$python" \
    "$comparator[backend,auth,storage,http,ai,realtime]" 'python-dotenv==1.2.4' 'uvicorn==0.54.0' 'psutil==7.2.2'
  bash "$runner" "$label" --profile --auth-ip-budget 400 --matrix \
    control:100:12:200 crud:20:15 crud:100:18:50 mixed:100:20:50 mixed:100:20:50 mixed:100:20:50 external:100:20:25
  exit
fi

"$scratch/uv" pip install --reinstall-package smackend --python "$python" \
  "$sealed[backend,auth,storage,http,ai,realtime,s3]" 'python-dotenv==1.2.4' 'uvicorn==0.54.0' 'psutil==7.2.2'

if [[ $mode == soak ]]; then
  bash "$runner" linux-soak --matrix mixed:100:60 mixed:100:1800
elif [[ $mode == capacity ]]; then
  bash "$runner" linux-pool4 --profile --pool 4 --matrix mixed:100:20:50 mixed:100:20:50
  bash "$runner" linux-capacity --auth-ip-budget 400 --matrix \
    crud:10:15 crud:20:15 crud:50:15 crud:100:20 crud:200:20 crud:500:20 \
    create-delete:50:18:25 login:100:60:5 signup:100:30:3 upload:50:12:20 download:100:12:50 \
    ai:50:15:25 stream:50:15:25 persistent:100:40 persistent:500:40 voice:10:10 voice:20:10 voice:50:10 voice:100:10
fi
