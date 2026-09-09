#!/usr/bin/env bash
# Tier 1 functional-test kit: fifteen scenarios, no LLM.
#
#   scripts/e2e-env.sh --reset
#   scripts/e2e-daemon.sh start
#   scripts/e2e-smoke.sh            # all fifteen
#   scripts/e2e-smoke.sh S2 S8      # just these
#
# Starts the daemon itself if one is not already up, and stops whatever it
# started — including on Ctrl-C and on a failing scenario.  A daemon that
# was already running when this script started is left alone.
#
# Refuses to run the scenarios at all unless the daemon can query its schema
# (scripts/e2e/probe.py), so a broken environment fails as one clear error
# rather than as fifteen scenarios that look like product regressions.
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

# Preflight.  `status` and `start` both gate on this already, but the world
# can be pulled out from under a healthy daemon between them and the first
# scenario — a concurrent `e2e-env.sh --reset` against the same
# AQ_E2E_HOME / E2E_DB_NAME terminates its backends and drops its database
# while it keeps answering `/api/health`.  Without this check the scenarios
# run anyway and fail with `relation "projects" does not exist`, which reads
# like fifteen product regressions in the capability report rather than one
# broken environment (task vivid-rapids).
if ! python3 "$REPO_ROOT/scripts/e2e/probe.py" --url "$AQ_E2E_API_URL"; then
    echo "refusing to run the scenarios against an unusable database" >&2
    exit 2
fi

PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$REPO_ROOT/scripts/e2e/smoke.py" "$@"
