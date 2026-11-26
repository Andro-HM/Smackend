#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
scratch="$repo/.local/phase4-linux"
here="$repo/experiments/phase4"
status="$scratch/soak.status"
: > "$scratch/followup.status"
trap 'code=$?; printf "%s\n" "$code" > "$scratch/followup.status"' EXIT
if [[ ${1:-} == wait-for-soak ]]; then
  for ((check=0; check<270; check++)); do
    if [[ -s "$status" ]]; then break; fi
    sleep 10
  done
  if [[ ! -s "$status" || $(cat "$status") != 0 ]]; then
    echo "The requested soak did not finish successfully." >&2
    exit 1
  fi
fi

runtime=$(cat "$scratch/runtime-dir")
python="$runtime/venv/bin/python"
evidence=${SMACKEND_QUAL_EVIDENCE_DIR:-"$here/evidence"}
mkdir -p "$evidence"
(set -o noclobber; "$python" "$here/driver_control.py" > "$evidence/curl-control.json")
bash "$here/linux-checks.sh" compare
bash "$here/linux-checks.sh" capacity
export SMACKEND_QUAL_PYTHON="$python" SMACKEND_QUAL_SCRATCH="${SMACKEND_QUAL_SCRATCH:-$(mktemp -d -t smackend-followup.XXXXXXXX)}"
export SMACKEND_QUAL_PG_PORT=55441
runner="$here/linux-local.sh"
bash "$runner" linux-workers1 --profile --workers 1 --pool 16 --matrix mixed:100:20:50 mixed:100:20:50
bash "$runner" linux-external-fresh --profile --matrix external:100:20:25 external:100:20:25
bash "$runner" linux-external-reuse --profile --reuse-outbound --matrix external:100:20:25 external:100:20:25
bash "$runner" linux-realtime --matrix \
  stream-long:100:20 voice-direct:50:12 voice-duplex:10:12 voice-duplex:20:12 voice-duplex:50:12 voice-duplex:100:12
