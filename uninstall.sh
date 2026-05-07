#!/usr/bin/env bash
# Uninstall agent-queue: returns the repo to a fresh-clone state and
# (optionally) removes the user config dir at ~/.agent-queue/.
#
# Usage: ./uninstall.sh [options]
#   -y, --yes           Skip all prompts; remove repo state AND user dir.
#   --keep-user-dir     Never touch ~/.agent-queue/ (overrides --yes for that dir).
#   --purge-user-dir    Remove ~/.agent-queue/ without prompting.
#   -h, --help          Show this help.

set -euo pipefail

# --- Self-protect: re-exec from a temp copy so `git clean` can't delete us mid-run ---
if [[ "${AQ_UNINSTALL_REENTRANT:-0}" != "1" ]]; then
    SELF_COPY="$(mktemp -t agent-queue-uninstall.XXXXXX)"
    cp "$0" "$SELF_COPY"
    chmod +x "$SELF_COPY"
    export AQ_UNINSTALL_REENTRANT=1
    export AQ_UNINSTALL_ORIG_DIR="$(cd "$(dirname "$0")" && pwd)"
    exec "$SELF_COPY" "$@"
fi
trap 'rm -f "$0"' EXIT

REPO_DIR="${AQ_UNINSTALL_ORIG_DIR}"
USER_DIR="${HOME}/.agent-queue"

# --- Argument parsing ---
ASSUME_YES=0
KEEP_USER_DIR=0
PURGE_USER_DIR=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1; PURGE_USER_DIR=1 ;;
        --keep-user-dir) KEEP_USER_DIR=1 ;;
        --purge-user-dir) PURGE_USER_DIR=1 ;;
        -h|--help)
            sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

# --- Verify we're in the agent-queue repo root ---
if [[ ! -f "$REPO_DIR/pyproject.toml" ]] || ! grep -q 'name = "agent-queue"' "$REPO_DIR/pyproject.toml"; then
    echo "Error: $REPO_DIR doesn't look like the agent-queue repo root." >&2
    exit 1
fi
if [[ ! -d "$REPO_DIR/.git" ]]; then
    echo "Error: $REPO_DIR is not a git working tree." >&2
    exit 1
fi

cd "$REPO_DIR"

echo "===================================================================="
echo "  agent-queue uninstall"
echo "===================================================================="
echo "Repo:     $REPO_DIR"
echo "User dir: $USER_DIR$([[ -d "$USER_DIR" ]] || echo ' (does not exist)')"
echo ""

# --- Stop running daemon if any ---
PID_FILE="$USER_DIR/daemon.pid"
if [[ -f "$PID_FILE" ]]; then
    DAEMON_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$DAEMON_PID" ]] && kill -0 "$DAEMON_PID" 2>/dev/null; then
        echo "Daemon is running (PID $DAEMON_PID)."
        if [[ $ASSUME_YES -eq 1 ]]; then
            stop_it="Y"
        else
            read -rp "Stop it? [Y/n] " stop_it
            stop_it="${stop_it:-Y}"
        fi
        if [[ "$stop_it" =~ ^[Yy]$ ]]; then
            kill "$DAEMON_PID" 2>/dev/null || true
            sleep 1
            kill -9 "$DAEMON_PID" 2>/dev/null || true
            echo "  ✓ daemon stopped"
        else
            echo "Aborting; please stop the daemon first." >&2
            exit 1
        fi
    fi
fi

# --- Decide on user dir ---
remove_user_dir=0
if [[ -d "$USER_DIR" ]]; then
    if [[ $KEEP_USER_DIR -eq 1 ]]; then
        remove_user_dir=0
    elif [[ $PURGE_USER_DIR -eq 1 ]]; then
        remove_user_dir=1
    else
        echo ""
        echo "$USER_DIR contains your config, database, logs, and Discord token."
        read -rp "Remove it too? [y/N] " ans
        [[ "${ans:-N}" =~ ^[Yy]$ ]] && remove_user_dir=1
    fi
fi

# --- Confirm repo nuke ---
if [[ $ASSUME_YES -ne 1 ]]; then
    echo ""
    echo "About to:"
    echo "  - git merge/rebase --abort   (if in-progress)"
    echo "  - git reset --hard HEAD       (drops uncommitted changes)"
    echo "  - git clean -ffdx             (removes ALL untracked + ignored: .venv, caches,"
    echo "                                 logs, *.db, .worktrees/, build artifacts, .claude/)"
    [[ $remove_user_dir -eq 1 ]] && echo "  - rm -rf $USER_DIR"
    echo "  - clean shell-completion side effects (~/.aq-complete.*, source line in rc files)"
    echo "  - pip uninstall agent-queue, agent-queue-api-client, memsearch  (best effort)"
    echo ""
    read -rp "Proceed? [y/N] " ans
    if [[ ! "${ans:-N}" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
fi

# --- Do it ---
echo ""
echo "Aborting any in-progress merge/rebase..."
git merge --abort  2>/dev/null || true
git rebase --abort 2>/dev/null || true
git cherry-pick --abort 2>/dev/null || true

echo "Resetting tracked files..."
git reset --hard HEAD

echo "Removing untracked + ignored files..."
git clean -ffdx

if [[ $remove_user_dir -eq 1 ]]; then
    echo "Removing $USER_DIR ..."
    rm -rf "$USER_DIR"
fi

echo "Removing CLI symlinks in ~/.local/bin (only those pointing into this repo)..."
for binname in aq agent-queue agent-queue-mcp; do
    link="$HOME/.local/bin/$binname"
    if [[ -L "$link" ]]; then
        target="$(readlink "$link" 2>/dev/null || true)"
        if [[ "$target" == "$REPO_DIR"/* ]]; then
            rm -f "$link"
            echo "  removed $link"
        fi
    fi
done

echo "Cleaning shell-completion artifacts..."
for f in "$HOME/.aq-complete.bash" "$HOME/.aq-complete.zsh" "$HOME/.config/fish/completions/aq.fish"; do
    if [[ -f "$f" ]]; then
        rm -f "$f"
        echo "  removed $f"
    fi
done
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [[ -f "$rc" ]] && grep -qE '# agent-queue setup\.sh|\.aq-complete\.' "$rc"; then
        cp "$rc" "$rc.aq-uninstall.bak"
        grep -vE '# agent-queue setup\.sh|\.aq-complete\.' "$rc.aq-uninstall.bak" > "$rc"
        echo "  stripped agent-queue lines from $rc (backup: $rc.aq-uninstall.bak)"
    fi
done

if command -v pip >/dev/null 2>&1; then
    echo "Best-effort pip uninstall (system-wide installs only)..."
    pip uninstall -y agent-queue agent-queue-api-client memsearch >/dev/null 2>&1 || true
fi

echo ""
echo "Done. To reinstall: ./setup.sh"
