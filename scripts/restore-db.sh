#!/usr/bin/env bash
# Restore a backup (Sprint 7, T7.4) — and the mandatory restore DRILL.
#
#   scripts/restore-db.sh --drill [backup.gpg]   decrypt latest (or given)
#       backup into a scratch dir, open it, run integrity checks + the test
#       suite pointed at it. Production untouched. Run monthly; record the
#       timing in docs/production-deployment.md §Restore.
#
#   scripts/restore-db.sh --restore backup.gpg   REPLACE the production DB
#       (stops the stack, swaps the file inside the volume, restarts).
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.prod.yml --env-file .env.production)
BACKUP_DIR=backups

log() { printf '[restore] %s\n' "$*"; }
die() { printf '[restore] ERROR: %s\n' "$*" >&2; exit 1; }

# shellcheck disable=SC2046
export $(grep -E '^PINCER_BACKUP_' .env.production | xargs) || true
: "${PINCER_BACKUP_PASSPHRASE:?PINCER_BACKUP_PASSPHRASE not set in .env.production}"

MODE="${1:-}"
FILE="${2:-}"
[ -n "$MODE" ] || die "usage: restore-db.sh --drill|--restore [backup.gpg]"

if [ -z "$FILE" ]; then
    FILE=$(ls -1t "${BACKUP_DIR}"/pincer-*.db.gpg 2>/dev/null | head -n1) || true
    [ -n "$FILE" ] || die "no backups in ${BACKUP_DIR}/ (fetch one from the offsite bucket first)"
fi
[ -f "$FILE" ] || die "backup file not found: $FILE"

decrypt() {
    gpg --batch --yes --quiet --decrypt \
        --passphrase "$PINCER_BACKUP_PASSPHRASE" -o "$1" "$FILE"
}

case "$MODE" in
--drill)
    START=$(date +%s)
    SCRATCH=$(mktemp -d)
    trap 'rm -rf "$SCRATCH"' EXIT
    log "Decrypting $FILE → scratch ..."
    decrypt "${SCRATCH}/pincer.db"
    log "Integrity check ..."
    python3 - "$SCRATCH/pincer.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "integrity_check failed"
tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print(f"integrity ok — {len(tables)} tables: {', '.join(sorted(tables)[:10])}...")
for t in ("voice_calls", "call_transcripts"):
    if t in tables:
        print(f"  {t}: {db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]} rows")
PY
    log "Running the test suite against the restored data dir ..."
    PINCER_DATA_DIR="$SCRATCH" uv run pytest -q --no-cov -x || die "test suite failed against restored DB"
    log "Restore drill PASSED in $(( $(date +%s) - START ))s — record this in the runbook."
    ;;
--restore)
    read -r -p "REPLACE the production database with $FILE? Type 'restore' to confirm: " answer
    [ "$answer" = "restore" ] || die "aborted"
    TMP=$(mktemp)
    decrypt "$TMP"
    log "Stopping the stack ..."
    "${COMPOSE[@]}" stop pincer pincer-tasks
    log "Swapping the database inside the volume ..."
    docker run --rm -v pincer_pincer-prod-data:/data -v "$TMP":/restore.db:ro alpine \
        sh -c 'cp /data/pincer.db /data/pincer.db.pre-restore 2>/dev/null; cp /restore.db /data/pincer.db && rm -f /data/pincer.db-wal /data/pincer.db-shm'
    rm -f "$TMP"
    log "Starting the stack ..."
    "${COMPOSE[@]}" up -d
    log "Done. Previous DB kept as pincer.db.pre-restore inside the volume. Verify: scripts/deploy.sh --verify"
    ;;
*)
    die "unknown mode: $MODE (use --drill or --restore)"
    ;;
esac
