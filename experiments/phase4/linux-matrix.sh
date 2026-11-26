#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
scratch="$repo/.local/phase4-linux"
runtime=$(mktemp -d -t smackend-linux-runtime.XXXXXXXX)
printf '%s\n' "$runtime" > "$scratch/runtime-dir"
python="$runtime/venv/bin/python"
"$scratch/uv" venv --python /usr/bin/python3 "$runtime/venv"
export SMACKEND_QUAL_PYTHON="$python"
export SMACKEND_QUAL_SCRATCH="$runtime"
export SMACKEND_QUAL_PG_PORT=55441
export UV_CACHE_DIR="$runtime/uv-cache"
export UV_LINK_MODE=copy
runner="$repo/experiments/phase4/linux-local.sh"
baseline="$repo/dist/smackend-0.1.0a7-py3-none-any.whl"
"$scratch/uv" pip install --python "$python" "$baseline[backend,auth,storage,http,ai,realtime]" \
  'Pillow==12.3.0' 'python-dotenv==1.2.4' 'uvicorn==0.54.0' 'psutil==7.2.2'

bash "$runner" linux-a7 --profile --auth-ip-budget 400 --matrix \
  control:100:12:200 crud:20:15 crud:100:18:50 mixed:100:20:50 mixed:100:20:50 mixed:100:20:50 \
  external:100:20:25
