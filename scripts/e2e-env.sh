#!/usr/bin/env bash
# Create or refresh the isolated environment the swarm e2e kit runs in.
#
#   scripts/e2e-env.sh            # idempotent: create what is missing
#   scripts/e2e-env.sh --reset    # additionally drop the DB, repo and vault
#   scripts/e2e-env.sh --register # register the projects + workspaces
#                                 # (needs a running daemon; `e2e-daemon.sh
#                                 #  start` does this for you)
#
# Everything lives under $AQ_E2E_HOME (default ~/.agent-queue-e2e): the
# daemon's data_dir, its vault, its workspaces, the throwaway git repo the
# project points at, the config, the pid file and the log.  Nothing here
# touches ~/.agent-queue or the `agent_queue` database.
#
# See docs/guides/e2e-swarm.md.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/e2e-common.sh
source "$REPO_ROOT/scripts/e2e-common.sh"

RESET=0
REGISTER_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --reset) RESET=1 ;;
        --register) REGISTER_ONLY=1 ;;
        -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Project + workspace registration
# ---------------------------------------------------------------------------
#
# Projects live in the *database*, not on disk, so this needs a running
# daemon — which is why it is a separate mode rather than part of the build
# above.  `e2e-daemon.sh start` calls it automatically once the API answers,
# so neither tier has to remember: Tier 1's scenarios and a Tier 2 operator
# both find `e2e` and `other` already registered.  Idempotent.

register_projects() {
    if ! curl -fsS --max-time 3 "$AQ_E2E_API_URL/api/health" >/dev/null 2>&1; then
        echo "no daemon at $AQ_E2E_API_URL — start it first" >&2
        return 1
    fi
    PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
        python3 "$REPO_ROOT/scripts/e2e/register.py"
}

# `--reset --register` is incoherent rather than merely ordered wrong: a
# reset drops the database out from under the daemon that registration
# needs, so whichever order it ran in, one half would be talking to a
# corpse.  Say so instead of guessing.
if [ "$REGISTER_ONLY" = "1" ] && [ "$RESET" = "1" ]; then
    echo "--reset and --register cannot be combined: the reset drops the database" >&2
    echo "the daemon is holding.  Run: e2e-env.sh --reset, then e2e-daemon.sh start" >&2
    echo "(which registers for you)." >&2
    exit 2
fi

if [ "$REGISTER_ONLY" = "1" ]; then
    register_projects
    exit $?
fi

# ---------------------------------------------------------------------------
# 1. Directories
# ---------------------------------------------------------------------------

#: Written on create; `--reset` refuses to delete a directory without it.
E2E_MARKER="$AQ_E2E_HOME/.aq-e2e"

# `rm -rf "$AQ_E2E_HOME"` runs on a path that comes from the environment,
# so a typo, an unset var expanded by a caller that did not `set -u`, or a
# copy-pasted `AQ_E2E_HOME=~` is one keystroke from deleting a home
# directory.  Two independent guards: the path may not *be* something
# precious, and it must carry this kit's marker file — so pointing the kit
# at an existing directory full of someone else's data refuses rather than
# eats it.
assert_safe_to_reset() {
    local target real
    target="$AQ_E2E_HOME"
    real="$(cd "$target" 2>/dev/null && pwd -P || true)"
    if [ -z "$real" ]; then
        echo "cannot resolve $target — refusing to reset" >&2
        exit 2
    fi

    local forbidden name
    for forbidden in "/" "$HOME" "$HOME/.agent-queue" "$REPO_ROOT"; do
        local forbidden_real
        forbidden_real="$(cd "$forbidden" 2>/dev/null && pwd -P || echo "$forbidden")"
        if [ "$real" = "$forbidden_real" ]; then
            echo "refusing to reset '$real' — that is $forbidden, not an e2e home" >&2
            exit 2
        fi
    done

    name="$(basename "$real")"
    if [ -z "$name" ] || [ "$name" = "/" ]; then
        echo "refusing to reset '$real' — empty basename" >&2
        exit 2
    fi

    if [ ! -f "$real/.aq-e2e" ]; then
        echo "refusing to reset '$real' — no .aq-e2e marker file." >&2
        echo "This directory was not created by scripts/e2e-env.sh.  If it really is" >&2
        echo "a disposable e2e home, remove it by hand and re-run." >&2
        exit 2
    fi
}

if [ "$RESET" = "1" ] && [ -d "$AQ_E2E_HOME" ]; then
    assert_safe_to_reset
    echo "==> resetting $AQ_E2E_HOME"
    rm -rf "$AQ_E2E_HOME"
fi

mkdir -p "$AQ_E2E_HOME"
# The marker is what makes the next `--reset` legal.  Written before
# anything else lands, so an interrupted build is still resettable.
printf '%s\n' \
    "Marker for scripts/e2e-env.sh.  Its presence is what allows --reset to" \
    "delete this directory.  Do not add anything here you want to keep." \
    > "$E2E_MARKER"
mkdir -p "$AQ_E2E_HOME/workspaces"
mkdir -p "$E2E_VAULT/agent-types"
mkdir -p "$E2E_VAULT/formulas"
mkdir -p "$E2E_BIN"

# An `aq` that resolves to this worktree, for anything that finds it on
# PATH.  Under Tier 2 that means the agents themselves: a session inherits
# the daemon's PATH through tmux, and the pip-installed `aq` would resolve
# `src` through the editable install — usually a different checkout, with
# no `aq task claim` in it at all, so every worker would fail its first
# command.  `e2e-daemon.sh start` puts this directory first on PATH.
cat > "$E2E_BIN/aq" <<WRAPPER
#!/usr/bin/env bash
# Generated by scripts/e2e-env.sh — the e2e \`aq\`, bound to
# $REPO_ROOT
exec python3 "$REPO_ROOT/scripts/e2e/aq.py" "\$@"
WRAPPER
chmod +x "$E2E_BIN/aq"

# ---------------------------------------------------------------------------
# 2. Throwaway git repo + one clone per pool slot
# ---------------------------------------------------------------------------
#
# `worktrees.enabled: false` in the config below means every git workspace
# behaves as `exclusive-clone`, so every session needs its own directory:
# two for the pool at `max_active: 2`, one for the replacement worker S2
# expects after a retirement, and two more for the push-scheduled task
# sessions S4's cooked children attract.

# The origin every workspace clones from is **bare**.  The completion
# pipeline commits and pushes on `aq task close`, and pushing to a
# non-bare repo whose `main` is checked out is rejected by git — which the
# pipeline reads as "unpushed commits", fails verification, and reopens the
# task.  That turned every S4 close into a reopen-and-relaunch loop.
seed_repo() {
    local bare="$1"
    local dir="${bare%.git}-seed"
    rm -rf "$dir"
    mkdir -p "$dir/e2e_pkg"
    cat > "$dir/README.md" <<'MD'
# e2e sample package

A throwaway repository for the Agent Queue swarm functional-test kit.
`tests/test_math.py` fails on purpose so a worker has something to fix.
MD
    cat > "$dir/e2e_pkg/__init__.py" <<'PY'
"""A one-function package, deliberately wrong."""


def add(a: int, b: int) -> int:
    return a - b
PY
    mkdir -p "$dir/tests"
    cat > "$dir/tests/test_math.py" <<'PY'
from e2e_pkg import add


def test_add():
    assert add(2, 2) == 4
PY
    git init -q --bare -b main "$bare"
    git -C "$dir" init -q -b main
    git -C "$dir" -c user.email=e2e@example.com -c user.name="AQ E2E" add -A
    git -C "$dir" -c user.email=e2e@example.com -c user.name="AQ E2E" \
        commit -q -m "seed the e2e sample package"
    git -C "$dir" remote add origin "$bare"
    git -C "$dir" push -q origin main
    rm -rf "$dir"
}

if [ ! -d "$E2E_REPO" ]; then
    echo "==> seeding bare git repo at $E2E_REPO"
    seed_repo "$E2E_REPO"
else
    echo "==> git repo already at $E2E_REPO"
fi

for n in 1 2 3 4 5; do
    ws="$AQ_E2E_HOME/workspaces/e2e-$n"
    if [ ! -d "$ws/.git" ]; then
        git clone -q "$E2E_REPO" "$ws"
        git -C "$ws" config user.email e2e@example.com
        git -C "$ws" config user.name "AQ E2E"
    fi
done

if [ ! -d "$E2E_OTHER_REPO" ]; then
    echo "==> seeding bare git repo at $E2E_OTHER_REPO"
    seed_repo "$E2E_OTHER_REPO"
fi
ws="$AQ_E2E_HOME/workspaces/other-1"
if [ ! -d "$ws/.git" ]; then
    git clone -q "$E2E_OTHER_REPO" "$ws"
    git -C "$ws" config user.email e2e@example.com
    git -C "$ws" config user.name "AQ E2E"
fi

# ---------------------------------------------------------------------------
# 3. Vault fixtures
# ---------------------------------------------------------------------------
#
# `vault_manager.ensure_layout()` seeds harnesses, workspace kinds and the
# stock agent types on first daemon boot.  What it does *not* know about is
# the pool profile this kit exercises, and the formulas S4 cooks.

echo "==> writing vault fixtures under $E2E_VAULT"

mkdir -p "$E2E_VAULT/agent-types/worker"
cat > "$E2E_VAULT/agent-types/worker/profile.md" <<'MD'
---
id: worker
name: "E2E Pool Worker"
tags: [profile, agent-type, e2e, pool]
---

# E2E Pool Worker

## Role
A pull-based pool worker for the swarm functional-test kit.  Claim a task,
do it, close it with `--claim-next`, repeat until the daemon says
`session_exhausted`, then `aq session drain-ack`.

Under Tier 1 (`sessions.provider: fake`) nothing actually reads this Role —
`scripts/e2e-smoke.sh` acts as the worker itself.  Under Tier 2 the real
`claude` harness reads it, so keep it accurate.

## Config
```json
{
  "harness": "claude",
  "lifecycle": "pool",
  "min_active": 0,
  "max_active": 2,
  "max_claims_per_session": 2,
  "needs_workspace": true,
  "workspaces": ["project-repo"]
}
```

## Tools
```json
{
  "allowed": ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite"]
}
```

## MCP Servers
```json
[]
```

## Rules
- Claim with `aq task claim --next`; the claim epoch is in `.aq/claim.json`.
- Close with `aq task close --outcome pass --summary "…" --claim-next`.
- On `session_exhausted` or `drain_requested`, run `aq session drain-ack`.
MD

# `review-and-fix` names `reviewer` and `coding` as node profiles; graph
# validation resolves both against the DB, so the vault must carry them.
for role in reviewer coding; do
    mkdir -p "$E2E_VAULT/agent-types/$role"
    cat > "$E2E_VAULT/agent-types/$role/profile.md" <<MD
---
id: $role
name: "E2E $role"
tags: [profile, agent-type, e2e]
---

# E2E $role

## Role
A task-lifecycle profile the formula fixtures route nodes to.  Present so
\`aq formula show\` / \`aq formula cook\` can resolve \`profile: $role\`.

## Config
\`\`\`json
{
  "harness": "claude",
  "lifecycle": "task",
  "needs_workspace": true,
  "workspaces": ["project-repo"]
}
\`\`\`

## Tools
\`\`\`json
{
  "allowed": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
}
\`\`\`

## MCP Servers
\`\`\`json
[]
\`\`\`
MD
done

cp "$REPO_ROOT/tests/fixtures/formulas/base-review.md" "$E2E_VAULT/formulas/base-review.md"
cp "$REPO_ROOT/tests/fixtures/formulas/review-and-fix.md" "$E2E_VAULT/formulas/review-and-fix.md"

# ---------------------------------------------------------------------------
# 4. Config
# ---------------------------------------------------------------------------
#
# One switch decides the tier.  `AQ_E2E_SESSION_PROVIDER=tmux` means real
# `claude` processes, and a live agent needs three subsystems Tier 1
# deliberately runs without: the message queue and named sessions (the
# supervisor chat the dashboard talks to), and playbooks (the default
# pipeline's worker-filed triage is what routes a filed task).  Under
# `fake` they stay off — each one either needs an LLM or adds chatter the
# deterministic runner would have to wait out.

if [ "$AQ_E2E_SESSION_PROVIDER" = "fake" ]; then
    TIER=1
    LIVE_SUBSYSTEMS=false
else
    TIER=2
    LIVE_SUBSYSTEMS=true
fi

echo "==> writing $E2E_CONFIG (tier $TIER, provider $AQ_E2E_SESSION_PROVIDER)"
cat > "$E2E_CONFIG" <<YAML
# Agent Queue — swarm e2e (tier $TIER) configuration.
# Generated by scripts/e2e-env.sh; edit that script, not this file.
#
# Isolation: its own data_dir, vault, database, API port and tmux socket.
# Nothing here can reach ~/.agent-queue or the \`agent_queue\` database.

data_dir: $AQ_E2E_HOME
workspace_dir: $AQ_E2E_HOME/workspaces

# No chat surface: this daemon is driven entirely by the CLI and the API.
messaging_platform: none

database:
  url: $E2E_DB_URL
  pool_min_size: 1
  pool_max_size: 8

health_check:
  enabled: true
  port: $AQ_E2E_PORT

mcp_server:
  enabled: true
  host: 127.0.0.1
  port: $AQ_E2E_PORT
  inject_into_tasks: true
  task_scope:
    enabled: false

# The whole point of the kit.
swarm:
  enabled: true
  claim_wait_max: 30
  max_starts_per_tick: 2
  max_drains_per_tick: 5
  # Long enough that no scenario races an incidental scale-down; S1/S2 assert
  # scale-*up*, and a drain in the middle would make them flaky.
  scale_down_grace: 3600
  prepare_timeout: 120
  max_filings_per_task: 20

sessions:
  enabled: true
  # Tier 1: nothing is spawned; the smoke runner acts as the pool worker.
  # Tier 2: switch to \`tmux\` (see docs/guides/e2e-swarm.md).
  provider: ${AQ_E2E_SESSION_PROVIDER}
  tmux_socket: $AQ_E2E_TMUX_SOCKET
  lease_ttl_seconds: 480
  adopt_on_start: false
  # \`claude\`'s first run in an unseen directory draws a "do you trust this
  # folder?" dialog, and on a cold cache it can take well over the stock 8s
  # to appear — long enough that the harness's auto-dismiss has already
  # given up, leaving the session parked on the dialog until a human
  # presses Enter.  45s is comfortably past that on a slow box and costs
  # nothing when the dialog never appears.
  dialog_budget_seconds: 45

# Every git workspace behaves as exclusive-clone, so a pool slot is just a
# directory.  Deterministic, and no worktree plumbing in the way of the
# claim protocol the kit is actually testing.
worktrees:
  enabled: false

work_graph:
  blocked_state_authoritative: false
  gate_sweep_interval_seconds: 5
  conditional_autoclose: true
  container_sweep_interval_seconds: 5

state_machine:
  enforce: false

# Tier 2 only.  playbooks: the default pipeline's worker-filed triage is
# what routes a task an agent files.  messages + supervisor_agent: the
# per-project supervisor sessions the dashboard's chat addresses
# (\`supervisor-<project>\`, or \`supervisor-global\`).  Under Tier 1 all three
# are off — each needs an LLM, and the scripted runner drives every one of
# their jobs itself.
playbooks:
  enabled: $LIVE_SUBSYSTEMS

messages:
  enabled: $LIVE_SUBSYSTEMS

supervisor_agent:
  enabled: $LIVE_SUBSYSTEMS

# Needs a network service (Milvus/ollama) either way.
memory:
  enabled: false

memory_extractor:
  enabled: false

llm_logging:
  enabled: false

inbox:
  enabled: false

logging:
  level: INFO
  format: text
  log_file: $AQ_E2E_HOME/daemon.log

api_auth:
  token_ttl_hours: 72
  require_session_token: false
YAML

# ---------------------------------------------------------------------------
# 5. Database
# ---------------------------------------------------------------------------

echo "==> preparing database $E2E_DB_NAME"
if [ "$RESET" = "1" ]; then
    python3 "$REPO_ROOT/scripts/e2e/dbsetup.py" "$E2E_ADMIN_DSN" "$E2E_DB_NAME" --reset
else
    python3 "$REPO_ROOT/scripts/e2e/dbsetup.py" "$E2E_ADMIN_DSN" "$E2E_DB_NAME"
fi

# ---------------------------------------------------------------------------
# 6. Validate the config the daemon will actually load
# ---------------------------------------------------------------------------

echo "==> validating config"
( cd "$REPO_ROOT" && python3 -m src.main "$E2E_CONFIG" --validate-config )

# ---------------------------------------------------------------------------
# 7. Summary
# ---------------------------------------------------------------------------

cat <<SUMMARY

Agent Queue swarm e2e environment
---------------------------------
  worktree      $REPO_ROOT
  home          $AQ_E2E_HOME
  config        $E2E_CONFIG
  vault         $E2E_VAULT
  database      $E2E_DB_NAME (on $(echo "$E2E_DB_URL" | sed 's#.*@##; s#/.*##'))
  api           $AQ_E2E_API_URL
  tmux socket   $AQ_E2E_TMUX_SOCKET
  provider      $AQ_E2E_SESSION_PROVIDER
  repo          $E2E_REPO  (project 'e2e')
  repo          $E2E_OTHER_REPO  (project 'other')
  log           $E2E_LOG

Next:  scripts/e2e-daemon.sh start  &&  scripts/e2e-smoke.sh
SUMMARY
