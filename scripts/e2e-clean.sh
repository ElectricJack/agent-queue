#!/usr/bin/env bash
# Destroy only the disposable resources created by e2e-env.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/e2e-common.sh
source "$REPO_ROOT/scripts/e2e-common.sh"

# Validate every operator-controlled target before stopping a process or
# invoking any cleanup command.  In particular, a stray daemon.pid in an
# unowned directory must never be enough to make e2e-daemon.sh signal that
# process.
if ! real_home="$(cd "$AQ_E2E_HOME" 2>/dev/null && pwd -P)"; then
    echo "cannot resolve e2e home '$AQ_E2E_HOME' — refusing cleanup" >&2
    exit 2
fi

for protected in "/" "$HOME" "$HOME/.agent-queue" "$REPO_ROOT"; do
    protected_real="$(cd "$protected" 2>/dev/null && pwd -P || printf '%s' "$protected")"
    if [ "$real_home" = "$protected_real" ]; then
        echo "refusing to destroy protected path '$real_home'" >&2
        exit 2
    fi
done
if [ ! -f "$real_home/.aq-e2e" ]; then
    echo "refusing to destroy '$real_home' — no .aq-e2e marker file" >&2
    exit 2
fi

# `aq` is the operator daemon's configured default socket, while `default`
# is tmux's own implicit server.  Neither can ever belong to this kit, even
# when the current run uses the fake provider and would not issue a tmux
# command itself.
case "$AQ_E2E_TMUX_SOCKET" in
    ""|aq|default)
        echo "refusing cleanup with protected tmux socket '$AQ_E2E_TMUX_SOCKET'" >&2
        exit 2
        ;;
esac

# Make the daemon helper resolve its pid file beneath the exact directory we
# just checked rather than through a non-canonical spelling supplied by the
# caller.
export AQ_E2E_HOME="$real_home"

"$REPO_ROOT/scripts/e2e-daemon.sh" stop

if [ "$AQ_E2E_SESSION_PROVIDER" = "tmux" ] && command -v tmux >/dev/null 2>&1; then
    tmux -L "$AQ_E2E_TMUX_SOCKET" kill-server 2>/dev/null || true
fi

python3 "$REPO_ROOT/scripts/e2e/dbsetup.py" "$E2E_ADMIN_DSN" "$E2E_DB_NAME" --drop
rm -rf -- "$real_home"
echo "destroyed disposable e2e home $real_home and database $E2E_DB_NAME"
