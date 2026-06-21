#!/bin/bash
set -euo pipefail

# restore-secrets.sh — decrypt and extract a homelab-secrets archive produced by
# backup-secrets.sh. Restores into a staging directory by default (never
# in-place over your live files), so you can review and copy what you need.
#
# Usage:
#   restore-secrets.sh <archive.tar.gz.age>                 extract to ./restored-secrets-<ts>/
#   restore-secrets.sh <archive.tar.gz.age> --into <dir>    extract to <dir>
#   restore-secrets.sh <archive.tar.gz.age> --list          list contents, write nothing
#   restore-secrets.sh --help
#
# Config (env overrides):
#   DECRYPT_CMD   decryption filter, takes the archive path, writes tar to
#                 stdout (default "age -d")
#
# Requires: age (sudo apt install age), tar, coreutils.

DECRYPT_CMD="${DECRYPT_CMD:-age -d}"
ARCHIVE=""
INTO=""
MODE=extract

log() { printf '==> %s\n' "$*"; }
die() {
  printf '[err] %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<EOF
Usage: $(basename "$0") <archive.tar.gz.age> [--into <dir>] [--list]

Decrypt and extract a homelab-secrets archive into a staging directory (never
in-place). Prompts for the age passphrase.

  --into <dir>  extract here instead of ./restored-secrets-<timestamp>/
  --list        list archive contents and exit (writes nothing)
  --help        this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --into)
      shift
      INTO="${1:?--into needs a path}"
      ;;
    --list) MODE=list ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      usage >&2
      die "unknown argument: $1"
      ;;
    *)
      [[ -z "$ARCHIVE" ]] || die "only one archive may be given"
      ARCHIVE="$1"
      ;;
  esac
  shift
done

[[ -n "$ARCHIVE" ]] || {
  usage >&2
  die "no archive given"
}
[[ -f "$ARCHIVE" ]] || die "archive not found: $ARCHIVE"
command -v tar >/dev/null 2>&1 || die "tar not found"
if [[ "$DECRYPT_CMD" == age* ]]; then
  command -v age >/dev/null 2>&1 || die "age not found — install it: sudo apt install age"
fi

if [[ "$MODE" == list ]]; then
  $DECRYPT_CMD "$ARCHIVE" | tar -tzf -
  exit 0
fi

target="${INTO:-./restored-secrets-$(date -u +%Y%m%dT%H%M%SZ)}"
umask 077
mkdir -p "$target"
$DECRYPT_CMD "$ARCHIVE" | tar -xzf - -C "$target"
find "$target" -type f -exec chmod 600 {} +
log "Restored into $target — review, then copy what you need into place."
