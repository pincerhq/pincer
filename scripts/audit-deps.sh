#!/usr/bin/env bash
# Dependency vulnerability gate (Sprint 8, T8.6).
#
# Python advisories come from pip-audit and are gated by
# scripts/check_pip_audit.py against .security/pip-audit-ignore.txt.
# Dashboard advisories are gated by `pnpm audit` at the critical level.
#
# Usage: scripts/audit-deps.sh
set -euo pipefail

cd "$(dirname "$0")/.."
IGNORE_FILE=".security/pip-audit-ignore.txt"
REPORT="$(mktemp -t pip-audit-XXXXXX.json)"
trap 'rm -f "$REPORT"' EXIT

echo "==> pip-audit (Python dependencies)"
# pip-audit exits non-zero when it finds anything; the triage gate decides,
# so only a missing/empty report is treated as a hard tooling failure here.
uv run --with pip-audit pip-audit --format=json --progress-spinner=off >"$REPORT" 2>/dev/null || true
if [ ! -s "$REPORT" ]; then
  echo "pip-audit produced no report — treating as failure." >&2
  exit 1
fi
uv run python scripts/check_pip_audit.py "$REPORT" "$IGNORE_FILE"

echo "==> pnpm audit (dashboard dependencies)"
(cd dashboard && pnpm audit --audit-level critical)

echo "All dependency audits passed."
