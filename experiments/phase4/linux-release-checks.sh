#!/usr/bin/env bash
set -u
here=$(cd "$(dirname "$0")" && pwd)
status=0
if [[ $# -eq 0 ]]; then set -- deployment rehearsal activation idle_probe; fi
for check in "$@"; do
  printf 'Checking %s\n' "$check"
  bash "$here/linux-reliability.sh" "$check" || status=1
done
exit "$status"
