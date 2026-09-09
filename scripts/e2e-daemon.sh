#!/usr/bin/env bash
# Start / stop / inspect the isolated e2e daemon.
#
#   scripts/e2e-daemon.sh start    # background, waits until /ready is ready
#   scripts/e2e-daemon.sh stop     # SIGTERM, then SIGKILL after a grace
#   scripts/e2e-daemon.sh status   # non-zero unless the schema is usable
#   scripts/e2e-daemon.sh logs [n]
#
# The daemon runs from this worktree (`python3 -m src.main <config>`) with
# the config scripts/e2e-env.sh generated.  See docs/guides/e2e-swarm.md.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/e2e-common.sh
source "$REPO_ROOT/scripts/e2e-common.sh"

STARTUP_TIMEOUT="${AQ_E2E_STARTUP_TIMEOUT:-90}"
STOP_GRACE="${AQ_E2E_STOP_GRACE:-20}"

# The pid in the file is only ours if the process *at* that pid is still
# the daemon.  A stale file plus a recycled pid means `stop` would signal
# whatever inherited the number — someone else's editor, or a build.  PIDs
# get reused quickly on a busy box and this file outlives crashes, so check
# the cmdline before believing it.
running_pid() {
    [ -f "$E2E_PID_FILE" ] || return 1
    local pid
    pid="$(cat "$E2E_PID_FILE" 2>/dev/null || true)"
    [ -n "$pid" ] || return 1
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    kill -0 "$pid" 2>/dev/null || return 1
    # /proc is Linux/WSL; on a kernel without it, fall back to trusting the
    # pid rather than refusing to manage the daemon at all.
    if [ -r "/proc/$pid/cmdline" ]; then
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "src.main" || return 1
    fi
    echo "$pid"
}

# Liveness only.  `/api/health` is a static stub that answers 200 as soon as
# uvicorn is listening; it never touches the database.  Useful to tell "a
# daemon owns this port" from "nothing is there", and useless as a readiness
# gate — see `ready_ok`.
health_ok() {
    curl -fsS --max-time 10 "$AQ_E2E_API_URL/api/health" >/dev/null 2>&1
}

# Readiness: the daemon can actually query its schema.  This is what `start`
# and `status` gate on, because a daemon whose schema setup failed, or whose
# database was dropped under it by a concurrent `e2e-env.sh --reset`, keeps
# serving `/api/health` while every scenario fails with `relation "projects"
# does not exist` (task vivid-rapids).
#
# Short timeout: this is the conditional the start loop polls once a second,
# and a daemon that has lost its database answers slowly (every check
# re-opens a connection and fails), so waiting the probe's full default here
# would stretch the startup budget.
ready_ok() {
    python3 "$REPO_ROOT/scripts/e2e/probe.py" --url "$AQ_E2E_API_URL" --quiet --timeout 10
}

# The same probe, printing what is wrong and what to do about it.  Runs with
# the probe's full timeout: this one is allowed to wait for the diagnosis.
ready_report() {
    python3 "$REPO_ROOT/scripts/e2e/probe.py" --url "$AQ_E2E_API_URL"
}

cmd_start() {
    if pid="$(running_pid)"; then
        if ready_ok; then
            echo "already running (pid $pid) at $AQ_E2E_API_URL"
            return 0
        fi
        # Reporting this as a successful start is how a broken world reaches
        # the scenarios: the caller believes it has a daemon and runs anyway.
        echo "a daemon is running (pid $pid) but is not usable:" >&2
        ready_report >&2 || true
        return 1
    fi
    if [ ! -f "$E2E_CONFIG" ]; then
        echo "no config at $E2E_CONFIG — run scripts/e2e-env.sh first" >&2
        return 1
    fi
    if health_ok; then
        echo "something is already answering on $AQ_E2E_API_URL — refusing to start" >&2
        return 1
    fi

    echo "==> starting daemon (config $E2E_CONFIG)"
    # `cd` into the worktree so `src` resolves here rather than through
    # whatever is pip-installed, and so alembic finds this checkout's
    # migrations.
    #
    # `$E2E_BIN` goes first on PATH and stays there: tmux sessions inherit
    # the daemon's environment, so under Tier 2 this is the `aq` the agents
    # themselves run.  Without it they get the pip-installed one, which
    # resolves `src` through the editable install — a different checkout,
    # with no `aq task claim` in it, so every worker fails its first
    # command.
    (
        cd "$REPO_ROOT"
        export PATH="$E2E_BIN:$PATH"
        exec python3 -m src.main "$E2E_CONFIG"
    ) >>"$E2E_LOG" 2>&1 &
    echo $! > "$E2E_PID_FILE"

    local waited=0
    while [ "$waited" -lt "$STARTUP_TIMEOUT" ]; do
        if ready_ok; then
            echo "==> ready after ${waited}s: $AQ_E2E_API_URL (pid $(cat "$E2E_PID_FILE"))"
            # Projects live in the database, so they cannot be part of
            # e2e-env.sh's build step.  Doing it here means both tiers find
            # them: Tier 1's scenarios assume them, and a Tier 2 operator
            # runs no smoke to create them.  Idempotent.
            #
            # A daemon that came up but could not be registered is not
            # usable and is not something the caller asked for, so take it
            # back down rather than leaving a half-built environment (and a
            # live pid file) behind after returning non-zero.
            if ! "$REPO_ROOT/scripts/e2e-env.sh" --register; then
                echo "registration failed — stopping the daemon we just started" >&2
                cmd_stop || true
                return 1
            fi
            return 0
        fi
        if ! running_pid >/dev/null; then
            echo "daemon exited during startup — last 40 log lines:" >&2
            tail -n 40 "$E2E_LOG" >&2 || true
            rm -f "$E2E_PID_FILE"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "daemon did not become ready within ${STARTUP_TIMEOUT}s" >&2
    ready_report >&2 || true
    tail -n 40 "$E2E_LOG" >&2 || true
    return 1
}

cmd_stop() {
    local pid
    if ! pid="$(running_pid)"; then
        rm -f "$E2E_PID_FILE"
        echo "not running"
        return 0
    fi
    echo "==> stopping daemon (pid $pid)"
    kill -TERM "$pid" 2>/dev/null || true
    local waited=0
    while [ "$waited" -lt "$STOP_GRACE" ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$E2E_PID_FILE"
            echo "==> stopped after ${waited}s"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "==> SIGTERM ignored for ${STOP_GRACE}s; SIGKILL" >&2
    kill -KILL "$pid" 2>/dev/null || true
    sleep 1
    rm -f "$E2E_PID_FILE"
}

cmd_status() {
    local pid
    if ! pid="$(running_pid)"; then
        echo "stopped   ($E2E_PID_FILE absent or stale)"
        return 1
    fi
    if ready_ok; then
        echo "running   pid $pid   ready at $AQ_E2E_API_URL"
        return 0
    fi
    # Non-zero for both remaining cases: `e2e-smoke.sh` uses this exit code to
    # decide whether it may reuse a daemon somebody else started, and a
    # daemon that cannot reach its schema is not reusable.
    if health_ok; then
        echo "running   pid $pid   answering but NOT usable at $AQ_E2E_API_URL"
        # On stdout: `status` is a reporting command, so its whole answer —
        # including why — belongs in the same stream as the summary line.
        ready_report 2>&1 || true
    else
        echo "running   pid $pid   NOT answering at $AQ_E2E_API_URL"
    fi
    return 1
}

cmd_logs() {
    tail -n "${1:-80}" "$E2E_LOG"
}

case "${1:-}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    logs)   shift; cmd_logs "$@" ;;
    *)      sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
