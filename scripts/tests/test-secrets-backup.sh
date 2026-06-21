#!/bin/bash
set -euo pipefail

# Round-trip + prune harness for the secrets DR scripts. Encryption is stubbed
# with cat (ENCRYPT_CMD/DECRYPT_CMD) so the bash logic runs without a TTY.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP="$SCRIPT_DIR/../backup-secrets.sh"
RESTORE="$SCRIPT_DIR/../restore-secrets.sh"
export RESTORE

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}
ok() { printf 'ok: %s\n' "$*"; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
root="$work/root"
dest="$work/dest"
mkdir -p "$root/ansible" "$root/terraform" "$dest"
printf 'vaultpw\n' >"$root/ansible/.vault_pass"
printf 'sshkey\n' >"$root/id_rsa"
printf 'tfvars\n' >"$root/terraform/terraform.tfvars"

cat >"$work/manifest" <<'EOF'
id_rsa                     # node key
ansible/.vault_pass        # crown jewel
terraform/terraform.tfvars
missing/nope.txt           # should warn, not abort
EOF

export HOMELAB_ROOT="$root" ENCRYPT_CMD="cat" DECRYPT_CMD="cat"

# --list shows a present entry and flags the missing one
out="$("$BACKUP" --list --manifest "$work/manifest" --dest "$dest")"
grep -q '.vault_pass' <<<"$out" || fail "--list should show .vault_pass"
grep -qi 'missing' <<<"$out" || fail "--list should flag the missing entry"
ok "--list"

# backup creates exactly one archive
"$BACKUP" --manifest "$work/manifest" --dest "$dest" >/dev/null
n="$(find "$dest" -name 'homelab-secrets-*.tar.gz.age' | wc -l)"
[[ "$n" -eq 1 ]] || fail "expected 1 archive, got $n"
ok "backup creates one archive"

# empty manifest aborts non-zero
printf '# nothing here\n' >"$work/empty"
if "$BACKUP" --manifest "$work/empty" --dest "$dest" >/dev/null 2>&1; then
  fail "expected non-zero exit on empty manifest"
fi
ok "empty manifest aborts"

# missing dest aborts non-zero
if "$BACKUP" --manifest "$work/manifest" --dest "$work/nope" >/dev/null 2>&1; then
  fail "expected non-zero exit on missing dest"
fi
ok "missing dest aborts"

echo "BACKUP TESTS PASSED"
