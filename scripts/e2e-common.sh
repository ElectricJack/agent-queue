#!/usr/bin/env bash
# Shared settings for the swarm e2e kit.  Sourced by e2e-env.sh,
# e2e-daemon.sh and e2e-smoke.sh; never run directly.
#
# Every value can be overridden from the environment, so two checkouts (or
# two people on one box) can run the kit side by side by exporting a
# different AQ_E2E_HOME and AQ_E2E_PORT.

# Where the whole isolated world lives: data_dir, vault, workspaces, the
# throwaway repos, the config, the pid file and the log.
AQ_E2E_HOME="${AQ_E2E_HOME:-$HOME/.agent-queue-e2e}"

# Its own API port, so a real daemon on 8081 keeps running undisturbed.
AQ_E2E_PORT="${AQ_E2E_PORT:-8099}"
AQ_E2E_API_URL="${AQ_E2E_API_URL:-http://127.0.0.1:$AQ_E2E_PORT}"

# Its own tmux server, so Tier 2 never adopts or kills the real one.
AQ_E2E_TMUX_SOCKET="${AQ_E2E_TMUX_SOCKET:-aq-e2e}"

# Tier 1 = fake (nothing spawned).  Tier 2 = tmux (real claude harnesses).
AQ_E2E_SESSION_PROVIDER="${AQ_E2E_SESSION_PROVIDER:-fake}"

# PostgreSQL.  `agent_queue` is the developer's real database and is never
# touched; the kit owns `agent_queue_e2e` alone.
E2E_PG_HOST="${E2E_PG_HOST:-localhost}"
E2E_PG_PORT="${E2E_PG_PORT:-5533}"
E2E_PG_USER="${E2E_PG_USER:-agent_queue}"
E2E_PG_PASSWORD="${E2E_PG_PASSWORD:-agent_queue_dev}"
E2E_DB_NAME="${E2E_DB_NAME:-agent_queue_e2e}"
E2E_PG_BASE="${E2E_PG_USER}:${E2E_PG_PASSWORD}@${E2E_PG_HOST}:${E2E_PG_PORT}"
# The daemon speaks asyncpg; dbsetup.py speaks plain libpq-style asyncpg.
E2E_DB_URL="postgresql+asyncpg://${E2E_PG_BASE}/${E2E_DB_NAME}"
E2E_ADMIN_DSN="postgresql://${E2E_PG_BASE}/postgres"

E2E_CONFIG="$AQ_E2E_HOME/config.yaml"
E2E_VAULT="$AQ_E2E_HOME/vault"
E2E_REPO="$AQ_E2E_HOME/repo.git"
E2E_OTHER_REPO="$AQ_E2E_HOME/repo-other.git"
E2E_ONBOARDING_ROOT="$AQ_E2E_HOME/onboarding"
E2E_LOG="$AQ_E2E_HOME/daemon.log"
E2E_PID_FILE="$AQ_E2E_HOME/daemon.pid"
# An `aq` on PATH that resolves to *this* worktree.  Prepended to the
# daemon's PATH by e2e-daemon.sh so that Tier 2 sessions — which inherit
# the daemon's environment through tmux — get the swarm commands.  The
# pip-installed `aq` resolves `src` through the editable install, which is
# usually a different checkout and has no `aq task claim` at all.
E2E_BIN="$AQ_E2E_HOME/bin"

export AQ_E2E_HOME AQ_E2E_PORT AQ_E2E_API_URL AQ_E2E_TMUX_SOCKET
export AQ_E2E_SESSION_PROVIDER
export E2E_CONFIG E2E_VAULT E2E_REPO E2E_OTHER_REPO E2E_LOG E2E_PID_FILE E2E_BIN
export E2E_ONBOARDING_ROOT
export E2E_DB_NAME E2E_DB_URL E2E_ADMIN_DSN

# `aq` from *this* worktree — see scripts/e2e/aq.py for why neither the
# installed console script nor `python3 -m src.cli.app` will do.
E2E_AQ=("python3" "$REPO_ROOT/scripts/e2e/aq.py")
export AQ_API_URL="$AQ_E2E_API_URL"

e2e_aq() {
    "${E2E_AQ[@]}" "$@"
}
