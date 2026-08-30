#!/usr/bin/env bash
# Tier 1 of the swarm functional-test kit: seven scenarios, no LLM.
#
#   scripts/e2e-env.sh --reset
#   scripts/e2e-daemon.sh start
#   scripts/e2e-smoke.sh            # all seven
#   scripts/e2e-smoke.sh S2 S7      # just these
#
# Starts the daemon itself if one is not already up, and stops whatever it
# started — including on Ctrl-C and on a failing scenario.  A daemon that
# was already running when this script started is left alone.
#
# Exits non-zero if any scenario fails.  See docs/guides/e2e-swarm.md.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/e2e-common.sh
source "$REPO_ROOT/scripts/e2e-common.sh"

STARTED_DAEMON=0

cleanup() {
    local rc=$?
    if [ "$STARTED_DAEMON" = "1" ]; then
        echo
        "$REPO_ROOT/scripts/e2e-daemon.sh" stop || true
    fi
    exit "$rc"
}
trap cleanup EXIT INT TERM

if [ ! -f "$E2E_CONFIG" ]; then
    echo "no config at $E2E_CONFIG — run scripts/e2e-env.sh first" >&2
    exit 2
fi

if "$REPO_ROOT/scripts/e2e-daemon.sh" status >/dev/null 2>&1; then
    echo "==> using the daemon already running at $AQ_E2E_API_URL"
else
    "$REPO_ROOT/scripts/e2e-daemon.sh" start || exit 1
    STARTED_DAEMON=1
fi

PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$REPO_ROOT/scripts/e2e/smoke.py" "$@"
