#!/bin/bash
set -euo pipefail

# backup-secrets.sh — bundle the workstation's git-ignored crown-jewel secrets,
# encrypt them with a passphrase (age), and write a timestamped archive to a
# backup destination (default: a NAS mount). Keeps the newest KEEP_LAST
# archives. Only PATHS live in the repo (backup-secrets.manifest) — never the
# secret contents.
#
# The script only READS local sources; it never writes or deletes anything on
# the local machine. Pruning is scoped strictly to archives in BACKUP_DEST.
#
# Usage:
#   backup-secrets.sh                 bundle + encrypt + copy to BACKUP_DEST
#   backup-secrets.sh --list          show the resolved manifest and exit
#   backup-secrets.sh --dest <dir>    override the destination
#   backup-secrets.sh --manifest <f>  override the manifest path
#   backup-secrets.sh --no-prune      keep all archives
#   backup-secrets.sh --help
#
# Config (env overrides):
#   HOMELAB_ROOT  base for relative manifest paths (default: the homelab root,
#                 i.e. the parent of the ansible repo)
#   BACKUP_DEST   destination dir (default: DEFAULT_DEST below)
#   KEEP_LAST     archives to retain (default 10; 0 = keep all)
#   ENCRYPT_CMD   encryption filter, stdin->stdout (default "age -p")
#
# Requires: age (sudo apt install age), tar, coreutils.

# --- EDIT ME: default backup destination (a NAS mount on this workstation) ---
DEFAULT_DEST="/mnt/nas/backups/homelab-secrets" # CHANGE ME to your NAS mount
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVE_PREFIX="homelab-secrets"

HOMELAB_ROOT="${HOMELAB_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
BACKUP_DEST="${BACKUP_DEST:-$DEFAULT_DEST}"
KEEP_LAST="${KEEP_LAST:-10}"
ENCRYPT_CMD="${ENCRYPT_CMD:-age -p}"
MANIFEST_FILE="${MANIFEST_FILE:-$SCRIPT_DIR/backup-secrets.manifest}"
PRUNE=true
MODE=run

log() { printf '==> %s\n' "$*"; }
warn() { printf '[skip] %s\n' "$*" >&2; }
die() {
  printf '[err] %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<EOF
Usage: $(basename "$0") [--list] [--dest <dir>] [--manifest <file>] [--no-prune]

Bundle the git-ignored secrets listed in the manifest into a timestamped,
age-encrypted archive and copy it to BACKUP_DEST, keeping the newest KEEP_LAST.

  --list            show the resolved manifest (present/missing) and exit
  --dest <dir>      destination dir (default: \$BACKUP_DEST or DEFAULT_DEST)
  --manifest <file> manifest path (default: alongside this script)
  --no-prune        keep all archives
  --help            this help

Manifest and env config are documented at the top of this script.
EOF
}

# --- Parse arguments -------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --list) MODE=list ;;
    --no-prune) PRUNE=false ;;
    --dest)
      shift
      BACKUP_DEST="${1:?--dest needs a path}"
      ;;
    --manifest)
      shift
      MANIFEST_FILE="${1:?--manifest needs a path}"
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
  shift
done

[[ -f "$MANIFEST_FILE" ]] || die "no manifest at $MANIFEST_FILE"

# --- Resolve the manifest against HOMELAB_ROOT -----------------------------
cd "$HOMELAB_ROOT"
present=()
missing=()
while IFS= read -r raw || [[ -n "$raw" ]]; do
  line="${raw%%#*}"                       # strip inline comments
  line="${line#"${line%%[![:space:]]*}"}" # trim leading whitespace
  line="${line%"${line##*[![:space:]]}"}" # trim trailing whitespace
  [[ -z "$line" ]] && continue
  line="${line/#\~/$HOME}" # expand a leading ~
  if matches="$(compgen -G "$line" 2>/dev/null)"; then
    while IFS= read -r m; do [[ -e "$m" ]] && present+=("$m"); done <<<"$matches"
  else
    missing+=("$line")
  fi
done <"$MANIFEST_FILE"

if [[ "$MODE" == list ]]; then
  log "Manifest: $MANIFEST_FILE (root: $HOMELAB_ROOT)"
  for f in "${present[@]:-}"; do [[ -n "$f" ]] && printf '  [present] %s\n' "$f"; done
  for f in "${missing[@]:-}"; do [[ -n "$f" ]] && printf '  [missing] %s\n' "$f"; done
  exit 0
fi

((${#missing[@]})) && for f in "${missing[@]}"; do warn "$f not found"; done
((${#present[@]})) || die "nothing to back up — manifest matched no existing files"

# --- Preflight -------------------------------------------------------------
[[ -d "$BACKUP_DEST" ]] || die "BACKUP_DEST '$BACKUP_DEST' does not exist (set DEFAULT_DEST or pass --dest)"
[[ -w "$BACKUP_DEST" ]] || die "BACKUP_DEST '$BACKUP_DEST' is not writable"
command -v tar >/dev/null 2>&1 || die "tar not found"
if [[ "$ENCRYPT_CMD" == age* ]]; then
  command -v age >/dev/null 2>&1 || die "age not found — install it: sudo apt install age"
fi

# --- Build a unique, timestamped archive name ------------------------------
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$BACKUP_DEST/${ARCHIVE_PREFIX}-${stamp}.tar.gz.age"
i=2
while [[ -e "$archive" ]]; do
  archive="$BACKUP_DEST/${ARCHIVE_PREFIX}-${stamp}-${i}.tar.gz.age"
  ((i++))
done

# --- Bundle (read-only) -> encrypt -> write atomically ---------------------
log "Bundling ${#present[@]} file(s) into $(basename "$archive")"
for f in "${present[@]}"; do printf '  - %s\n' "$f"; done

umask 077
# Stage inside BACKUP_DEST so the final mv is a same-filesystem atomic rename
# (mktemp in /tmp would make mv a non-atomic cross-device copy that can leave a
# truncated archive at the final name if interrupted).
tmp="$(mktemp -p "$BACKUP_DEST")"
trap 'rm -f "$tmp"' EXIT
# cwd is HOMELAB_ROOT, so the stored paths are relative and restore cleanly.
tar -czf - "${present[@]}" | $ENCRYPT_CMD >"$tmp"
mv "$tmp" "$archive"
trap - EXIT
chmod 600 "$archive"
log "Wrote $archive ($(du -h "$archive" | cut -f1))"

# --- Prune: NAS archives only, never local sources -------------------------
if $PRUNE && [[ "$KEEP_LAST" -gt 0 ]]; then
  # Archive names are timestamped (no spaces), so ls -t ordering is safe.
  # shellcheck disable=SC2012
  mapfile -t archives < <(ls -1t "$BACKUP_DEST/${ARCHIVE_PREFIX}-"*.tar.gz.age 2>/dev/null)
  if ((${#archives[@]} > KEEP_LAST)); then
    log "Pruning $((${#archives[@]} - KEEP_LAST)) old archive(s); keeping newest $KEEP_LAST"
    for old in "${archives[@]:KEEP_LAST}"; do
      rm -f "$old"
      warn "removed $(basename "$old")"
    done
  fi
fi

kept="$(find "$BACKUP_DEST" -maxdepth 1 -name "${ARCHIVE_PREFIX}-*.tar.gz.age" | wc -l)"
log "Done — ${kept} archive(s) in $BACKUP_DEST"
