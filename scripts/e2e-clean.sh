#!/usr/bin/env bash
# Destroy only the disposable resources created by e2e-env.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/e2e-common.sh
source "$REPO_ROOT/scripts/e2e-common.sh"

"$REPO_ROOT/scripts/e2e-daemon.sh" stop

if [ "$AQ_E2E_SESSION_PROVIDER" = "tmux" ] && command -v tmux >/dev/null 2>&1; then
    tmux -L "$AQ_E2E_TMUX_SOCKET" kill-server 2>/dev/null || true
fi

real_home="$(realpath -m "$AQ_E2E_HOME")"
case "$real_home" in
    /|"$HOME"|"$HOME/.agent-queue"|"$REPO_ROOT")
        echo "refusing to destroy protected path '$real_home'" >&2
        exit 2
        ;;
esac
if [ ! -f "$real_home/.aq-e2e" ]; then
    echo "refusing to destroy '$real_home' — no .aq-e2e marker file" >&2
    exit 2
fi

python3 "$REPO_ROOT/scripts/e2e/dbsetup.py" "$E2E_ADMIN_DSN" "$E2E_DB_NAME" --drop
rm -rf -- "$real_home"
echo "destroyed disposable e2e home $real_home and database $E2E_DB_NAME"
