#!/usr/bin/env bash
# Bootstrap the common AQ installer on the supported Windows/WSL2 host.
# This is invoked by install-windows.ps1 but is also useful when a user has
# already opened Ubuntu.  It deliberately has no Windows-side mutations.
set -euo pipefail

repository="${1:-https://github.com/ElectricJack/agent-queue.git}"
checkout_dir="${AQ_CHECKOUT_DIR:-$HOME/.local/share/agent-queue}"

next_action() {
    printf '\nNext: %s\n' "$*" >&2
}

if [[ ! -r /proc/version ]] || ! grep -qiE 'microsoft|wsl' /proc/version; then
    printf 'This bootstrap runs only inside WSL2. Start Ubuntu from Windows, then rerun it.\n' >&2
    exit 12
fi

if [[ "$(uname -r)" != *[Ww][Ss][Ll]2* ]] && [[ ! -e /proc/sys/fs/binfmt_misc/WSLInterop ]]; then
    printf 'WSL1 is not supported. From Windows run: wsl --set-version Ubuntu-24.04 2\n' >&2
    exit 12
fi

if [[ "$checkout_dir" == /mnt/* ]]; then
    printf 'AQ must be installed in the Linux filesystem, not %s. Set AQ_CHECKOUT_DIR under $HOME and rerun.\n' "$checkout_dir" >&2
    exit 12
fi

if ! command -v apt-get >/dev/null; then
    printf 'Ubuntu 24.04 is the supported WSL distribution. Install it from Windows with: wsl --install -d Ubuntu-24.04\n' >&2
    exit 12
fi

# A fresh Ubuntu WSL install has Python, but venv and Git are not guaranteed.
# They are Linux-side prerequisites, so this prompt is for sudo in the WSL
# distro, never for a Windows administrator password.
if ! command -v git >/dev/null || ! python3 -m venv --help >/dev/null 2>&1; then
    printf 'Installing the WSL prerequisites (Git and Python venv support)...\n'
    sudo apt-get update
    sudo apt-get install -y git python3-venv
fi

if [[ -x "$checkout_dir/.venv/bin/aq" ]]; then
    aq_command="$checkout_dir/.venv/bin/aq"
elif command -v aq >/dev/null; then
    aq_command="$(command -v aq)"
else
    if [[ -e "$checkout_dir" && ! -d "$checkout_dir/.git" ]]; then
        printf '%s exists but is not an AQ checkout; refusing to overwrite it.\n' "$checkout_dir" >&2
        exit 20
    fi
    if [[ ! -d "$checkout_dir/.git" ]]; then
        mkdir -p "$(dirname "$checkout_dir")"
        git clone --depth 1 "$repository" "$checkout_dir"
    else
        printf 'Reusing existing AQ checkout at %s (it is not reset or overwritten).\n' "$checkout_dir"
    fi

    python3 -m venv "$checkout_dir/.venv"
    "$checkout_dir/.venv/bin/pip" install --upgrade pip
    "$checkout_dir/.venv/bin/pip" install -e "$checkout_dir[cli]"
    aq_command="$checkout_dir/.venv/bin/aq"
fi

# Persist only the user-level launcher location, never a Windows path.  The
# line is idempotent and `.profile` is read by login shells and WSL terminals.
path_line='export PATH="$HOME/.local/bin:$PATH" # agent-queue'
if ! grep -qF "$path_line" "$HOME/.profile" 2>/dev/null; then
    printf '\n%s\n' "$path_line" >> "$HOME/.profile"
fi
export PATH="$HOME/.local/bin:$PATH"

printf 'Running the common AQ installer inside WSL2...\n'
"$aq_command" install --interactive

next_action "Continue in this Ubuntu terminal. If AQ later reports a dashboard URL, open that localhost URL in your Windows browser. WSL can reach a Windows-only service at: ip route show default | awk '{print \$3}'."
