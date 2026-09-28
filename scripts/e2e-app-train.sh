#!/usr/bin/env bash
# The App-mode integration train proof (App-mode spec §10, scenarios S1-S10)
# against a disposable GitHub repository.
#
#   scripts/e2e-app-train.sh env        # PG container + isolated AQ home + App config
#   scripts/e2e-app-train.sh start      # the App-only daemon (no gh login, no tokens)
#   scripts/e2e-app-train.sh run STEP   # one step of scripts/e2e/app_train.py
#   scripts/e2e-app-train.sh aq ARGS    # the operator CLI against the disposable daemon
#   scripts/e2e-app-train.sh stop | status | logs [n]
#   scripts/e2e-app-train.sh destroy    # stop, remove the container (keeps the home)
#
# Steps, in order (each is resumable and records every id and SHA in
# $AQ_E2E_HOME/evidence.jsonl, and every GitHub payload it read in
# $AQ_E2E_HOME/payloads/):
#
#   prepare   onboard the fixture project, import the shared routes, render
#             the policy, manifest, audit workflow and ruleset; read-only on GitHub
#   s1 s3 s2  anchors, preflight negatives, protection reader (+ S9 guard)
#   train     bind the policy, observe -> hierarchy -> train
#   s4 s5 s6 s8 s10 s7
#
# Every step from s1 on mutates the fixture.  They refuse to run until
# $AQ_E2E_HOME/APPROVED holds the operator's approval (spec §10: the operator
# approves the live run before its first GitHub mutation).
#
# Isolation (spec §10): its own PostgreSQL container, its own AQ home, the fake
# session provider (this script plays every pool worker), and a daemon whose
# environment has an empty GH_CONFIG_DIR and no GitHub token, so every GitHub
# call it makes goes through the App configured in the operator's config.  The
# harness itself uses the operator's `gh` login for the anchors only the
# repository admin may write, and for the negative controls.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------
# The disposable world.  Everything is overridable from the environment.
# ---------------------------------------------------------------------------

export AQ_E2E_HOME="${AQ_E2E_HOME:-$HOME/.agent-queue-app-train}"
export AQ_E2E_PORT="${AQ_E2E_PORT:-8157}"
export AQ_E2E_TMUX_SOCKET="${AQ_E2E_TMUX_SOCKET:-aq-app-train}"
export AQ_E2E_SESSION_PROVIDER=fake
export E2E_PG_HOST=127.0.0.1
export E2E_PG_PORT="${AQ_APP_TRAIN_PG_PORT:-5557}"
export E2E_PG_USER=agent_queue
export E2E_PG_PASSWORD="${AQ_APP_TRAIN_PG_PASSWORD:-agent_queue_app_train}"
export E2E_DB_NAME="${AQ_APP_TRAIN_DB_NAME:-aq_app_train_e2e}"
AQ_APP_TRAIN_PG_CONTAINER="${AQ_APP_TRAIN_PG_CONTAINER:-aq-app-train-pg}"
AQ_APP_TRAIN_PG_IMAGE="${AQ_APP_TRAIN_PG_IMAGE:-postgres:18-alpine}"

# The fixture and the App (spec §10).  The App's four keys come from the
# operator's config: proving the production registration is the point.
export AQ_APP_TRAIN_REPOSITORY="${AQ_APP_TRAIN_REPOSITORY:-ElectricJack/aq-gh615-app-fixture-20260923}"
export AQ_APP_TRAIN_OPERATOR_CONFIG="${AQ_APP_TRAIN_OPERATOR_CONFIG:-$HOME/.agent-queue/config.yaml}"

# Variables a worker session carries (src/sessions/env.py DAEMON_ENV_STRIP_KEYS)
# plus every ambient GitHub credential.  Neither the daemon nor the operator
# CLI below may inherit them.
SESSION_KEYS=(
    AQ_SESSION_ID AQ_TASK_ID AQ_PROJECT_ID AQ_PROFILE AQ_DAEMON_EPOCH AQ_INSTANCE_TOKEN
    AQ_WORK_DIR AQ_API_URL AQ_API_TOKEN AQ_DB_SCOPE AQ_DATABASE_URL AGENT_QUEUE_DB
    AQ_SESSION_KIND AQ_SESSION_NAME AQ_PROFILE_ID AQ_AGENT_ID AQ_STARTUP_PROMPT_DELIVERED
    AQ_CLAIM_EPOCH AQ_CPU_SHARE AQ_CPU_CORES AQ_TEST_SLOTS AQ_TEST_WORKERS AQ_CONFIG_PATH
)
GITHUB_KEYS=(
    GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN
    SSH_AUTH_SOCK GIT_ASKPASS SSH_ASKPASS
)
for key in "${SESSION_KEYS[@]}"; do unset "$key"; done

# shellcheck source=scripts/e2e-common.sh
source "$REPO_ROOT/scripts/e2e-common.sh"

GH_EMPTY="$AQ_E2E_HOME/gh-empty"
APP_CONFIG="$AQ_E2E_HOME/app-mode.yaml"

# The daemon's environment: App credentials only.  An empty GH_CONFIG_DIR means
# `gh` has no login, no global or system Git config means no credential helper,
# and the GitHub token variables are gone (the #615 App-only rerun's isolation).
app_only() {
    local unset_args=()
    for key in "${GITHUB_KEYS[@]}"; do unset_args+=(-u "$key"); done
    env "${unset_args[@]}" \
        GH_CONFIG_DIR="$GH_EMPTY" \
        GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_NOSYSTEM=1 \
        GIT_TERMINAL_PROMPT=0 \
        GIT_AUTHOR_NAME="AQ App Train" GIT_AUTHOR_EMAIL="aq-app-train@example.invalid" \
        GIT_COMMITTER_NAME="AQ App Train" GIT_COMMITTER_EMAIL="aq-app-train@example.invalid" \
        "$@"
}

pg_container_up() {
    if docker inspect -f '{{.State.Running}}' "$AQ_APP_TRAIN_PG_CONTAINER" 2>/dev/null | grep -q true; then
        return 0
    fi
    if docker inspect "$AQ_APP_TRAIN_PG_CONTAINER" >/dev/null 2>&1; then
        echo "==> starting existing container $AQ_APP_TRAIN_PG_CONTAINER"
        docker start "$AQ_APP_TRAIN_PG_CONTAINER" >/dev/null
    else
        echo "==> creating disposable PostgreSQL $AQ_APP_TRAIN_PG_CONTAINER on 127.0.0.1:$E2E_PG_PORT"
        docker run -d --name "$AQ_APP_TRAIN_PG_CONTAINER" \
            --label aq.purpose=app-train-e2e \
            -e POSTGRES_USER="$E2E_PG_USER" -e POSTGRES_PASSWORD="$E2E_PG_PASSWORD" \
            -p "127.0.0.1:$E2E_PG_PORT:5432" "$AQ_APP_TRAIN_PG_IMAGE" >/dev/null
    fi
    local waited=0
    until docker exec "$AQ_APP_TRAIN_PG_CONTAINER" pg_isready -U "$E2E_PG_USER" >/dev/null 2>&1; do
        waited=$((waited + 1))
        if [ "$waited" -gt 60 ]; then
            echo "PostgreSQL in $AQ_APP_TRAIN_PG_CONTAINER never became ready" >&2
            return 1
        fi
        sleep 1
    done
}

# The four `integration.github_app` keys, copied from the operator's config.
# The private key is referenced where it is, never copied or read here.
write_app_config() {
    python3 - "$AQ_APP_TRAIN_OPERATOR_CONFIG" "$APP_CONFIG" <<'PY'
import sys
import yaml

source, target = sys.argv[1:3]
app = ((yaml.safe_load(open(source)) or {}).get("integration") or {}).get("github_app")
if not app:
    sys.exit(f"{source} configures no integration.github_app: App mode is the point of this proof")
keys = ("client_id", "app_id", "installation_id", "private_key_path")
missing = [key for key in keys if not app.get(key)]
if missing:
    sys.exit(f"{source} integration.github_app lacks {', '.join(missing)}")
with open(target, "w") as fh:
    fh.write("\n# App credential mode (scripts/e2e-app-train.sh; App-mode spec §10).\n")
    yaml.safe_dump({"integration": {"github_app": {key: app[key] for key in keys}}}, fh)
print(f"==> App {app['app_id']} installation {app['installation_id']} from {source}")
PY
}

cmd_env() {
    mkdir -p "$AQ_E2E_HOME" "$GH_EMPTY"
    chmod 700 "$GH_EMPTY"
    pg_container_up
    write_app_config
    AQ_E2E_EXTRA_CONFIG="$APP_CONFIG" "$REPO_ROOT/scripts/e2e-env.sh"
    PYTHONPATH="$REPO_ROOT" python3 "$REPO_ROOT/scripts/e2e/app_train.py" write-profiles
}

cmd_start() {
    # Prove the isolation before the daemon inherits it.
    if app_only gh auth status >/dev/null 2>&1; then
        echo "gh still has a login under the App-only environment; refusing to start" >&2
        return 1
    fi
    echo "==> App-only environment: gh has no login (GH_CONFIG_DIR=$GH_EMPTY), no GitHub token"
    pg_container_up
    app_only "$REPO_ROOT/scripts/e2e-daemon.sh" start
}

case "${1:-}" in
    env) cmd_env ;;
    start) cmd_start ;;
    stop) "$REPO_ROOT/scripts/e2e-daemon.sh" stop ;;
    status) "$REPO_ROOT/scripts/e2e-daemon.sh" status ;;
    logs) shift; "$REPO_ROOT/scripts/e2e-daemon.sh" logs "$@" ;;
    aq) shift; e2e_aq "$@" ;;
    run)
        shift
        PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
            python3 "$REPO_ROOT/scripts/e2e/app_train.py" "$@"
        ;;
    destroy)
        "$REPO_ROOT/scripts/e2e-daemon.sh" stop || true
        docker rm -f "$AQ_APP_TRAIN_PG_CONTAINER" >/dev/null 2>&1 || true
        echo "removed $AQ_APP_TRAIN_PG_CONTAINER; $AQ_E2E_HOME is kept"
        ;;
    *) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
