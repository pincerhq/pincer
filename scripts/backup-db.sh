#!/usr/bin/env bash
# Nightly SQLite backup (Sprint 7, T7.4):
#   online .backup (WAL-safe) → gpg-encrypted → local ./backups → optional
#   S3-compatible EU bucket via rclone → prune > retention days.
#
# Cron (as the deploy user):  15 3 * * *  /opt/pincer/scripts/backup-db.sh
#
# Requires in .env.production: PINCER_BACKUP_PASSPHRASE (gpg symmetric key);
# optional PINCER_BACKUP_RCLONE_REMOTE (e.g. "hetzner-s3:pincer-backups") and
# PINCER_BACKUP_RETENTION_DAYS (default 30).
#
# AN UNTESTED BACKUP IS NOT A BACKUP — run scripts/restore-db.sh --drill
# after changing anything here (see docs/production-deployment.md §Restore).
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.prod.yml --env-file .env.production)
BACKUP_DIR=backups
STAMP=$(date -u +%Y%m%d-%H%M%S)
NAME="pincer-${STAMP}.db"

log() { printf '[backup] %s\n' "$*"; }
die() { printf '[backup] ERROR: %s\n' "$*" >&2; exit 1; }

# shellcheck disable=SC2046
export $(grep -E '^PINCER_BACKUP_' .env.production | xargs) || true
: "${PINCER_BACKUP_PASSPHRASE:?PINCER_BACKUP_PASSPHRASE not set in .env.production}"
RETENTION_DAYS="${PINCER_BACKUP_RETENTION_DAYS:-30}"
REMOTE="${PINCER_BACKUP_RCLONE_REMOTE:-}"

mkdir -p "$BACKUP_DIR"

# Online, WAL-safe snapshot via sqlite3's backup API inside the container
log "Snapshotting /app/data/pincer.db (online .backup) ..."
"${COMPOSE[@]}" exec -T pincer python - <<'PY'
import sqlite3
src = sqlite3.connect("/app/data/pincer.db")
dst = sqlite3.connect("/app/data/backup-tmp.db")
with dst:
    src.backup(dst)
dst.close(); src.close()
print("snapshot ok")
PY

docker cp "$("${COMPOSE[@]}" ps -q pincer)":/app/data/backup-tmp.db "${BACKUP_DIR}/${NAME}"
"${COMPOSE[@]}" exec -T pincer rm -f /app/data/backup-tmp.db

log "Encrypting → ${BACKUP_DIR}/${NAME}.gpg"
gpg --batch --yes --symmetric --cipher-algo AES256 \
    --passphrase "$PINCER_BACKUP_PASSPHRASE" \
    -o "${BACKUP_DIR}/${NAME}.gpg" "${BACKUP_DIR}/${NAME}"
rm -f "${BACKUP_DIR}/${NAME}"

if [ -n "$REMOTE" ]; then
    command -v rclone >/dev/null || die "rclone not installed but PINCER_BACKUP_RCLONE_REMOTE is set"
    log "Uploading to ${REMOTE} ..."
    rclone copy "${BACKUP_DIR}/${NAME}.gpg" "${REMOTE}/"
    log "Pruning remote copies older than ${RETENTION_DAYS}d ..."
    rclone delete --min-age "${RETENTION_DAYS}d" "${REMOTE}/" || true
fi

log "Pruning local copies older than ${RETENTION_DAYS}d ..."
find "$BACKUP_DIR" -name 'pincer-*.db.gpg' -mtime "+${RETENTION_DAYS}" -delete

log "Done: ${BACKUP_DIR}/${NAME}.gpg ($(du -h "${BACKUP_DIR}/${NAME}.gpg" | cut -f1))"
