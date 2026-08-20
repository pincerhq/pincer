#!/usr/bin/env bash
# One-command rollback to the previously deployed image tag (Sprint 7, T7.5).
#
#   scripts/rollback.sh              → previous entry in .deploy_history
#   scripts/rollback.sh sha-abc1234  → a specific recorded tag
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.prod.yml --env-file .env.production)
HISTORY_FILE=.deploy_history

log() { printf '\033[1;33m[rollback]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[rollback] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
    [ -f "$HISTORY_FILE" ] || die "no $HISTORY_FILE — nothing recorded to roll back to"
    # Second-to-last deployed tag
    TARGET=$(awk '{print $2}' "$HISTORY_FILE" | tail -n 2 | head -n 1)
    CURRENT=$(awk '{print $2}' "$HISTORY_FILE" | tail -n 1)
    [ -n "$TARGET" ] && [ "$TARGET" != "$CURRENT" ] || die "no previous deployment recorded"
fi

docker image inspect "pincer/pincer:${TARGET}" >/dev/null 2>&1 || die "image pincer/pincer:${TARGET} not present"

log "Rolling back to pincer/pincer:${TARGET} ..."
PINCER_IMAGE_TAG="${TARGET}" "${COMPOSE[@]}" up -d --remove-orphans
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ${TARGET} (rollback)" >> "$HISTORY_FILE"
log "Done. Verify: scripts/deploy.sh --verify"
