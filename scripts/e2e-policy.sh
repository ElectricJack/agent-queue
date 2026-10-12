#!/usr/bin/env bash
# Real-daemon policy acceptance; owns separate child kits, never the smoke daemon.
# Run e2e-env.sh --reset and e2e-smoke.sh first with the same unique settings.
set -euo pipefail
: "${AQ_E2E_HOME:?choose a unique disposable home}"
: "${AQ_E2E_PORT:?choose a unique port}"
: "${E2E_DB_NAME:?choose a unique e2e database name}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/scripts/e2e-common.sh"
cd "$REPO_ROOT"
exec python3 -m scripts.e2e.policy_acceptance "$@"
