#!/usr/bin/env bash
# Production deploy (Sprint 7) — one command from a clean checkout to a
# running, TLS-valid, doctor-green instance.
#
#   scripts/deploy.sh            build + gate + deploy + verify
#   scripts/deploy.sh --verify   post-deploy verification only
#
# The deploy REFUSES to start when `pincer doctor --production` reports any
# CRITICAL check (T7.3). Every deployed git SHA is appended to .deploy_history
# for scripts/rollback.sh.
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.prod.yml --env-file .env.production)
ENV_FILE=.env.production
HISTORY_FILE=.deploy_history

log() { printf '\033[1;32m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$ENV_FILE" ] || die "$ENV_FILE missing — copy .env.production.example and fill it in"
perms=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE")
[ "$perms" = "600" ] || die "$ENV_FILE must be chmod 600 (is $perms)"

# shellcheck disable=SC1090
API_DOMAIN=$(grep -E '^PINCER_API_DOMAIN=' "$ENV_FILE" | cut -d= -f2)
VOICE_DOMAIN=$(grep -E '^PINCER_VOICE_DOMAIN=' "$ENV_FILE" | cut -d= -f2)

# Sprint 8 (T8.1): an UNAUTHENTICATED relay connect must be refused with 403.
#
# This check does double duty. A 403 proves both that the TLS proxy forwards
# the WebSocket upgrade to the app (the Hotfix-2 / Twilio-64101 failure was a
# broken upgrade path) AND that the app rejects a connect with no signed token.
# A 101 would mean anyone on the internet can open a socket into a live call;
# a 404/502/timeout means the upgrade never reaches the app at all.
verify_relay_upgrade() {
    log "Verifying the relay WSS upgrade is reachable AND refuses unsigned connects ..."
    code=$(curl -sS -o /dev/null --max-time 10 -w '%{http_code}' \
        -H 'Connection: Upgrade' \
        -H 'Upgrade: websocket' \
        -H 'Sec-WebSocket-Version: 13' \
        -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
        "https://${VOICE_DOMAIN}/api/apps/twilio/relay" 2>/dev/null || echo "000")
    case "$code" in
        403)
            log "Relay WSS OK: upgrade reaches the app and unsigned connects are refused (403)"
            ;;
        101|200)
            die "SECURITY: the relay accepted an UNSIGNED WebSocket connect (HTTP $code). \
Check PINCER_VOICE_WS_AUTH_REQUIRED and PINCER_TWILIO_AUTH_TOKEN."
            ;;
        000)
            log "WARN: relay upgrade timed out — the proxy may not be forwarding WebSockets (Twilio 64101 risk)"
            ;;
        *)
            log "WARN: relay upgrade returned HTTP $code (expected 403) — verify routing manually"
            ;;
    esac
}

verify() {
    log "Verifying https://${API_DOMAIN}/api/health ..."
    curl -fsS --max-time 10 "https://${API_DOMAIN}/api/health" >/dev/null || die "API health check failed"
    log "Verifying https://${VOICE_DOMAIN}/voice/health ..."
    curl -fsS --max-time 10 "https://${VOICE_DOMAIN}/voice/health" >/dev/null \
        || log "WARN: /voice/health not reachable (ok only if voice is disabled)"
    verify_relay_upgrade
    log "Verification done."
}

if [ "${1:-}" = "--verify" ]; then
    verify
    exit 0
fi

GIT_SHA=$(git rev-parse --short HEAD)
IMAGE_TAG="sha-${GIT_SHA}"
log "Building pincer/pincer:${IMAGE_TAG} (git ${GIT_SHA}) ..."
docker build --target runtime -t "pincer/pincer:${IMAGE_TAG}" -t pincer/pincer:prod .

log "Running the production doctor gate ..."
if ! docker run --rm --env-file "$ENV_FILE" "pincer/pincer:${IMAGE_TAG}" pincer doctor --production; then
    die "doctor --production is RED — refusing to deploy (fix the CRITICAL checks above)"
fi

log "Deploying ..."
PINCER_IMAGE_TAG="${IMAGE_TAG}" "${COMPOSE[@]}" up -d --remove-orphans

log "Waiting for the pincer container to become healthy (up to 120s) ..."
for _ in $(seq 1 24); do
    status=$("${COMPOSE[@]}" ps --format '{{.Health}}' pincer 2>/dev/null || echo "")
    [ "$status" = "healthy" ] && break
    sleep 5
done
[ "$status" = "healthy" ] || die "pincer container did not become healthy — check: ${COMPOSE[*]} logs pincer"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ${IMAGE_TAG}" >> "$HISTORY_FILE"
verify
log "Deployed ${IMAGE_TAG}. Rollback: scripts/rollback.sh"
