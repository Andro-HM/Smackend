#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
scratch="$repo/.local/phase4-linux"
runtime=$(cat "$scratch/runtime-dir")
python="$runtime/venv/bin/python"
runner="$repo/experiments/phase4/linux-local.sh"
evidence=${SMACKEND_QUAL_EVIDENCE_DIR:-"$repo/experiments/phase4/evidence"}
mode=${1:?Pass deployment, rehearsal, activation, or idle_probe}
if [[ ! $mode =~ ^(deployment|rehearsal|activation|idle_probe)$ ]]; then exit 2; fi
trap 'code=$?; printf "%s\n" "$code" > "$scratch/$mode.status"' EXIT
mkdir -p "$evidence"
target=$(mktemp -d -t "smackend-${mode}.XXXXXXXX")
export SMACKEND_QUAL_PYTHON="$python"
export SMACKEND_QUAL_PG_PORT=55442
if [[ $mode == activation ]]; then
  (set -o noclobber; "$python" "$repo/experiments/phase4/activation_matrix.py" "$target/application" > "$evidence/$mode.json")
else
  bash "$runner" "${mode//_/-}" -- bash -c 'set -o noclobber; "$1" "$2" "$3" > "$4"' _ \
    "$python" "$repo/experiments/phase4/$mode.py" "$target/application" "$evidence/$mode.json"
fi
