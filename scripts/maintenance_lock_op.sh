#!/bin/bash
# Verify, hold or release a maintenance lock a failed CI run left on VM300.
# Runbook: docs/maintenance-lock-release.md. Never prints the nonce or passes it in argv.
# Usage, as root: timeout 20 bash <dir>/maintenance_lock_op.sh <expected owner> <expected stage> verify|hold|release
# <dir> holds this script and maintenance_lock.py from one reviewed ansible commit, and nothing else.
set -euo pipefail
umask 077
R=/var/lib/homelab-maintenance
I=/sys/class/dmi/id/product_uuid
W=Runner.Worker
if [ "$EUID" -eq 0 ]; then
  PATH=/usr/sbin:/usr/bin:/sbin:/bin
else
  # Test hooks (scripts/tests/test_maintenance_lock_op.py), honoured only when not root.
  R="${LOCK_OP_TEST_ROOT:-$R}"
  I="${LOCK_OP_TEST_INSTANCE_FILE:-$I}"
  W="${LOCK_OP_TEST_WORKER:-$W}"
fi
D="$(cd "$(dirname "$0")" && pwd)"
H="$D/maintenance_lock.py"
if [ $# -ne 3 ]; then
  echo "usage: maintenance_lock_op.sh <expected owner> <expected stage> verify|hold|release"
  exit 2
fi
want_owner="$1" want_stage="$2" action="$3"
case "$want_stage" in
  acquired | applying | recovering | held) ;;
  *)
    echo "UNKNOWN STAGE: stop"
    exit 2
    ;;
esac
case "$action" in
  verify | hold | release) ;;
  *)
    echo "unknown action"
    exit 2
    ;;
esac
rc=0
pgrep -x "$W" >/dev/null || rc=$?
if [ "$rc" -eq 0 ]; then
  echo "RUNNER JOB ALIVE: wait for it to end, then run again"
  exit 4
elif [ "$rc" -ne 1 ]; then
  echo "pgrep failed: stop"
  exit 1
fi
if [ ! -e "$R/operation.json" ]; then
  echo "NO LOCK: nothing to do"
  exit 6
fi
if [ "$want_stage" = held ] && [ ! -e "$D/held-by-me" ]; then
  echo "HELD BY SOMEONE ELSE: stop"
  exit 5
fi
# The record's instance is checked against the VM's own UUID, as roles/maintenance_runner asserts it.
HOMELAB_RUNNER_INSTANCE="$(tr -d '[:space:]' <"$I" | tr '[:upper:]' '[:lower:]')"
MAINTENANCE_OWNER="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["owner"])' "$R/operation.json")"
MAINTENANCE_NONCE="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["nonce"])' "$R/operation.json")"
export HOMELAB_RUNNER_INSTANCE MAINTENANCE_OWNER MAINTENANCE_NONCE
test -n "$HOMELAB_RUNNER_INSTANCE"
test -n "$MAINTENANCE_OWNER"
test -n "$MAINTENANCE_NONCE"
if [ "$MAINTENANCE_OWNER" != "$want_owner" ]; then
  echo "OWNER MISMATCH: stop"
  exit 3
fi
python3 -I "$H" verify --root "$R" --stage "$want_stage"
echo "verify ok: $want_owner at $want_stage"
case "$action" in
  verify) ;;
  hold)
    python3 -I "$H" advance --root "$R" --stage "$want_stage" --next-stage held
    touch "$D/held-by-me"
    echo "advanced to held"
    ;;
  release)
    python3 -I "$H" release --root "$R"
    rm -f "$D/held-by-me"
    if [ -e "$R/operation.json" ]; then
      echo "released; a NEW record exists now (another owner?): read it with 2.1"
    else
      echo "released: operation.json gone"
    fi
    ;;
esac
