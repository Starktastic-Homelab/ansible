#!/bin/bash
set -euo pipefail

# Round-trip + prune harness for the secrets DR scripts. Encryption is stubbed
# with cat (ENCRYPT_CMD/DECRYPT_CMD) so the bash logic runs without a TTY.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP="$SCRIPT_DIR/../backup-secrets.sh"
RESTORE="$SCRIPT_DIR/../restore-secrets.sh"

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

# round-trip: restore the archive and diff against the originals
arch="$(find "$dest" -name 'homelab-secrets-*.tar.gz.age' | head -1)"
"$RESTORE" "$arch" --into "$work/restored" >/dev/null
diff "$root/ansible/.vault_pass" "$work/restored/ansible/.vault_pass" || fail ".vault_pass mismatch"
diff "$root/id_rsa" "$work/restored/id_rsa" || fail "id_rsa mismatch"
diff "$root/terraform/terraform.tfvars" "$work/restored/terraform/terraform.tfvars" || fail "tfvars mismatch"
ok "round-trip diff clean"

# restore staging dir must be private (0700), not world-traversable
perms="$(stat -c '%a' "$work/restored")"
[[ "$perms" == "700" ]] || fail "restore staging dir should be 700, got $perms"
ok "restore staging dir is private"

# --list on restore shows the entries without writing anything
list="$("$RESTORE" "$arch" --list)"
grep -q 'ansible/.vault_pass' <<<"$list" || fail "restore --list should show .vault_pass"
ok "restore --list"

# prune keeps exactly KEEP_LAST archives in the dest
prunedir="$work/prune"
mkdir -p "$prunedir"
for _ in 1 2 3 4 5; do "$BACKUP" --manifest "$work/manifest" --dest "$prunedir" >/dev/null; done
KEEP_LAST=3 "$BACKUP" --manifest "$work/manifest" --dest "$prunedir" >/dev/null
kept="$(find "$prunedir" -name 'homelab-secrets-*.tar.gz.age' | wc -l)"
[[ "$kept" -eq 3 ]] || fail "expected 3 archives after prune, got $kept"
ok "prune keep-last"

echo "ALL TESTS PASSED"
