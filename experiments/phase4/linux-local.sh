#!/usr/bin/env bash
# Linux qualification without installing system packages or starting a host service.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
scratch="$repo/.local/phase4-linux"
mkdir -p "$scratch"

if [[ ${1:-} == bootstrap ]]; then
  cd "$scratch"
  apt-get download postgresql-16 postgresql-client-16 libpq5
  for package in ./*.deb; do dpkg-deb -x "$package" root; done
  python3 - <<'PY'
import io
import json
from pathlib import Path
from urllib.request import urlopen
import zipfile

with urlopen("https://pypi.org/pypi/uv/0.10.11/json", timeout=30) as response:
    entries = json.load(response)["urls"]
entry = next(row for row in entries if "manylinux" in row["filename"] and "x86_64" in row["filename"])
with urlopen(entry["url"], timeout=60) as response:
    payload = response.read()
import hashlib
assert hashlib.sha256(payload).hexdigest() == entry["digests"]["sha256"]
with zipfile.ZipFile(io.BytesIO(payload)) as archive:
    executable = next(name for name in archive.namelist() if name.endswith("/scripts/uv"))
    Path("uv").write_bytes(archive.read(executable))
PY
  chmod +x uv
  export UV_CACHE_DIR="$scratch/uv-cache"
  ./uv venv --python /usr/bin/python3 "$scratch/venv"
  wheel="$repo/dist/smackend-0.1.0a7-py3-none-any.whl"
  ./uv pip install --python "$scratch/venv/bin/python" "$wheel[backend,auth,storage,s3,http,ai,realtime]" \
    'python-dotenv==1.2.4' 'uvicorn==0.54.0' 'psutil==7.2.2' 'pytest>=9,<10' 'moto[s3]==5.2.6' pip-audit
  exit
fi

label=${1:?Provide an output label and one or more workload scenarios}
shift
if [[ ! $label =~ ^[a-z0-9-]+$ ]]; then exit 2; fi
pg="$scratch/root/usr/lib/postgresql/16/bin"
export LD_LIBRARY_PATH="$scratch/root/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
python="${SMACKEND_QUAL_PYTHON:-$scratch/venv/bin/python}"
data=$(mktemp -d -t smackend-phase4.XXXXXXXX)
port=${SMACKEND_QUAL_PG_PORT:-55440}
password=$("$python" -c 'from secrets import token_hex; print(token_hex(24))')
printf '%s\n' "$password" > "$data/password"
"$pg/initdb" -D "$data/db" -L "$scratch/root/usr/share/postgresql/16" --locale=C.UTF-8 \
  --encoding=UTF8 -U smackend --auth-local=trust --auth-host=scram-sha-256 --pwfile="$data/password" >/dev/null
rm -- "$data/password"
"$pg/pg_ctl" -D "$data/db" -l "$data/postgres.log" \
  -o "-h 127.0.0.1 -k $data -p $port -c max_connections=50" -w start
trap '"$pg/pg_ctl" -D "$data/db" -m fast -w stop' EXIT
export SMACKEND_SMOKE_DSN="postgresql://smackend:$password@127.0.0.1:$port/postgres"
export SMACKEND_POSTGRES_BIN="$pg"
export SMACKEND_QUAL_PG_DATA="$data/db"
export SMACKEND_QUAL_PG_SOCKET="$data"
if [[ ${1:-} == -- ]]; then shift; "$@"; exit; fi
evidence=${SMACKEND_QUAL_EVIDENCE_DIR:-"$repo/experiments/phase4/evidence"}
"$python" "$repo/experiments/phase4/run.py" --scratch "${SMACKEND_QUAL_SCRATCH:-$scratch}/$label" \
  --evidence "$evidence/$label" --database-pid "$(head -n1 "$data/db/postmaster.pid")" "$@"
