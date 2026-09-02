#!/usr/bin/env bash
# Run the dashboard dev server against the e2e daemon (see docs/guides/e2e-swarm.md).
#
# Usage: scripts/e2e-dashboard.sh            # foreground, Ctrl-C to stop
#   AQ_E2E_PORT overrides the daemon port (default 8099, matching e2e-common.sh);
#   DASHBOARD_PORT overrides the Vite port (default 5173).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

AQ_E2E_PORT="${AQ_E2E_PORT:-8099}"
DASHBOARD_PORT="${DASHBOARD_PORT:-5173}"

export AQ_API_TARGET="http://127.0.0.1:${AQ_E2E_PORT}"
export VITE_WS_URL="ws://127.0.0.1:${AQ_E2E_PORT}"

if [[ ! -d "$ROOT_DIR/node_modules" ]]; then
    echo "node_modules missing — run: npm install" >&2
    exit 1
fi
if [[ ! -d "$ROOT_DIR/packages/aq-ts-client/src" ]]; then
    echo "TS client not generated — run: ./scripts/regenerate-ts-client.sh --from-file" >&2
    exit 1
fi

echo "Dashboard → http://127.0.0.1:${DASHBOARD_PORT}  (API proxy → ${AQ_API_TARGET})"
cd "$ROOT_DIR/dashboard"
exec npx vite --host 127.0.0.1 --port "$DASHBOARD_PORT"
