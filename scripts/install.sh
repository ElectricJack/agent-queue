#!/usr/bin/env bash
# Download-and-run bootstrap for Agent Queue.
#
#     curl -fsSL https://raw.githubusercontent.com/ElectricJack/agent-queue/main/scripts/install.sh | bash
#
# This is a *transport*, not a second installer: it gets a supported host to
# the point where `aq` exists, then hands every machine decision --
# prerequisites, PostgreSQL, agent CLIs and their logins, configuration, the
# daemon and the dashboard -- to the one common installer, `aq install`.
# Do not add installation policy here; see docs/reference/cli/install.md.
#
# It runs on the two hosts the support matrix accepts (src/install/platform.py):
# macOS 14+ natively, and Ubuntu 24.04 on WSL2 for Windows.  Windows users
# reach it through scripts/install-windows.ps1, which enables WSL first.
#
# Exit codes mirror `aq install`:
#   0  ready          10  needs_user (a human-only action)
#   12 unsupported_host   20 failed
set -euo pipefail

repository="${1:-https://github.com/ElectricJack/agent-queue.git}"
checkout_dir="${AQ_CHECKOUT_DIR:-$HOME/.local/share/agent-queue}"
local_bin="$HOME/.local/bin"

next_action() {
    printf '\nNext: %s\n' "$*" >&2
}

fail_unsupported() {
    printf '%s\n' "$*" >&2
    exit 12
}

# --- Which host is this? -----------------------------------------------------
#
# The matrix is enforced again inside `aq install`; refusing here as well keeps
# an unsupported machine from being touched at all, and reports the observed
# fact rather than a guess.
system="$(uname -s)"
case "$system" in
    Darwin) host="macos" ;;
    Linux)
        if [[ ! -r /proc/version ]] || ! grep -qiE 'microsoft|wsl' /proc/version; then
            fail_unsupported "This Linux host is not a WSL2 distribution; AQ's supported Linux path is Windows + WSL2. See docs/tutorials/install.md."
        fi
        host="wsl"
        ;;
    *)
        fail_unsupported "Unsupported operating system: $system. Supported hosts are macOS 14+ and Windows 10 2004+/11 with WSL2."
        ;;
esac

# --- Host-specific prerequisites --------------------------------------------
#
# Only enough to create a virtualenv and run `aq`.  Anything AQ can install
# itself is left to `aq install`, and anything that needs a password is a
# needs_user checkpoint -- this script never types one.
if [[ "$host" == "macos" ]]; then
    macos_version="$(sw_vers -productVersion 2>/dev/null || echo "")"
    macos_major="${macos_version%%.*}"
    if [[ -z "$macos_major" ]] || (( macos_major < 14 )); then
        fail_unsupported "AQ requires macOS 14 (Sonoma) or newer; this Mac reports '${macos_version:-unknown}'."
    fi

    # Git and the compilers Homebrew's own installer needs both come from the
    # Command Line Tools, and their installer is a macOS dialog.
    if ! xcode-select --print-path >/dev/null 2>&1; then
        printf 'The macOS Command Line Tools are not installed.\n' >&2
        next_action "Run 'xcode-select --install', accept the macOS dialog, then rerun this command."
        exit 10
    fi

    python_bin=""
    for candidate in python3.13 python3.12; do
        if command -v "$candidate" >/dev/null 2>&1; then
            python_bin="$candidate"
            break
        fi
    done
    if [[ -z "$python_bin" ]] && command -v python3 >/dev/null 2>&1; then
        if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
            python_bin="python3"
        fi
    fi

    if [[ -z "$python_bin" ]]; then
        # Homebrew installs without sudo once it exists, but installing
        # Homebrew itself asks for an administrator password.  That is the
        # user's to type, exactly as `aq install`'s macos.homebrew step has it.
        if command -v brew >/dev/null 2>&1; then
            printf 'Installing Python 3.12 with Homebrew...\n'
            brew install python@3.12
            python_bin="$(brew --prefix python@3.12)/bin/python3.12"
        else
            printf 'AQ needs Python 3.12+, and this Mac has neither it nor Homebrew.\n' >&2
            next_action "Install Homebrew with the official command below -- it asks for your administrator password, which AQ never types -- then rerun this command.
  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
            exit 10
        fi
    fi
else
    if [[ "$(uname -r)" != *[Ww][Ss][Ll]2* ]] && [[ ! -e /proc/sys/fs/binfmt_misc/WSLInterop ]]; then
        fail_unsupported "WSL1 is not supported. From Windows run: wsl --set-version Ubuntu-24.04 2"
    fi
    if [[ "$checkout_dir" == /mnt/* ]]; then
        fail_unsupported "AQ must be installed in the Linux filesystem, not $checkout_dir. Set AQ_CHECKOUT_DIR under \$HOME and rerun."
    fi
    if ! command -v apt-get >/dev/null; then
        fail_unsupported "Ubuntu 24.04 is the supported WSL distribution. Install it from Windows with: wsl --install -d Ubuntu-24.04"
    fi

    # A fresh Ubuntu WSL install has Python, but venv and Git are not
    # guaranteed.  This sudo prompt belongs to the WSL distribution, never to a
    # Windows administrator.
    if ! command -v git >/dev/null || ! python3 -m venv --help >/dev/null 2>&1; then
        printf 'Installing the WSL prerequisites (Git and Python venv support)...\n'
        sudo apt-get update
        sudo apt-get install -y git python3-venv
    fi
    python_bin="python3"
fi

# --- Check out AQ, keep it current, and build its virtualenv -----------------
#
# Rerunning the one command is the documented way to resume *and* to pick up a
# fix, so a rerun must not keep running whatever it cloned the first time.
if [[ -e "$checkout_dir" && ! -d "$checkout_dir/.git" ]]; then
    printf '%s exists but is not an AQ checkout; refusing to overwrite it.\n' "$checkout_dir" >&2
    exit 20
fi

if [[ ! -d "$checkout_dir/.git" ]] && command -v aq >/dev/null; then
    # An `aq` from somewhere else -- typically a contributor's own checkout,
    # built with ./setup.sh.  It is not this script's to update or replace.
    aq_command="$(command -v aq)"
    printf 'Using the aq already on PATH at %s.\n' "$aq_command"
else
    if [[ ! -d "$checkout_dir/.git" ]]; then
        mkdir -p "$(dirname "$checkout_dir")"
        git clone --depth 1 "$repository" "$checkout_dir"
    elif [[ -n "$(git -C "$checkout_dir" status --porcelain --untracked-files=no)" ]]; then
        # Somebody edited tracked files in the installer's checkout.  Never
        # discard that; run what is there and say so.
        printf 'Not updating %s: it has local changes to tracked files.\n' "$checkout_dir" >&2
    elif ! git -C "$checkout_dir" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
        printf 'Not updating %s: it has no upstream branch to follow.\n' "$checkout_dir" >&2
    else
        before="$(git -C "$checkout_dir" rev-parse --short HEAD)"
        # The clone is shallow, so the old and new tips share no history a
        # merge could fast-forward across.  The tree is clean (checked above)
        # and .venv is ignored, so moving to the upstream tip loses nothing.
        if git -C "$checkout_dir" fetch --depth 1 --quiet origin \
            && git -C "$checkout_dir" reset --hard --quiet '@{u}'; then
            after="$(git -C "$checkout_dir" rev-parse --short HEAD)"
            if [[ "$before" == "$after" ]]; then
                printf 'AQ checkout at %s is up to date (%s).\n' "$checkout_dir" "$after"
            else
                printf 'Updated AQ checkout at %s (%s -> %s).\n' "$checkout_dir" "$before" "$after"
            fi
        else
            printf 'Could not update %s (offline?); continuing with %s.\n' "$checkout_dir" "$before" >&2
        fi
    fi

    if [[ ! -x "$checkout_dir/.venv/bin/python" ]]; then
        "$python_bin" -m venv "$checkout_dir/.venv"
    fi

    # Install from *inside* the checkout.  pyproject's `cli` extra names the
    # generated API client as a PEP 508 direct reference with a relative path
    # ("agent-queue-api-client @ file:packages/aq-client"), and pip resolves
    # that against the current working directory rather than the project being
    # installed.  A bootstrap is run from wherever the user happens to be, so
    # resolving it from here looked for packages/aq-client under *that*
    # directory and failed with an OSError.  The subshell keeps the cd local.
    #
    # This runs on every pass, not only the first: an update can change the
    # dependencies, and pip is quick when everything is already satisfied.
    (
        cd "$checkout_dir"
        ./.venv/bin/pip install --quiet --upgrade pip
        ./.venv/bin/pip install --quiet -e "./packages/aq-client"
        ./.venv/bin/pip install --quiet -e ".[cli]"
    )
    aq_command="$checkout_dir/.venv/bin/aq"
fi

# --- Put `aq` on PATH for the shells that come after this one ----------------
#
# Exporting PATH alone would lose the command the moment this script exits, and
# a PATH entry with nothing linked into it is worse than none: link the
# launchers first, then make the directory durable.
mkdir -p "$local_bin"
for binname in aq agent-queue; do
    if [[ -x "$checkout_dir/.venv/bin/$binname" ]]; then
        ln -sf "$checkout_dir/.venv/bin/$binname" "$local_bin/$binname"
    fi
done

# zsh (the macOS default) reads .zprofile at login and never reads .profile.
case "$(basename "${SHELL:-sh}")" in
    zsh) profile_file="$HOME/.zprofile" ;;
    *)   profile_file="$HOME/.profile" ;;
esac
path_line='export PATH="$HOME/.local/bin:$PATH" # agent-queue'
if ! grep -qF "$path_line" "$profile_file" 2>/dev/null; then
    printf '\n%s\n' "$path_line" >> "$profile_file"
    printf 'Added %s to your PATH in %s.\n' "$local_bin" "$profile_file"
fi
export PATH="$local_bin:$PATH"

# --- Hand over to the one common installer -----------------------------------
printf 'Running the common AQ installer (aq install)...\n'
#
# Reattach the terminal before handing over.  Under `curl ... | bash` this
# script *is* stdin, so a wizard that prompted on the inherited stdin read the
# remaining script text as its answers -- "Error: invalid input" per leftover
# line, then Abort at end of file.  /dev/tty is the controlling terminal
# regardless of what stdin was piped from.
#
# Only the child's stdin is redirected, never this shell's: bash reads a piped
# script incrementally, so an `exec < /dev/tty` here could truncate the rest of
# this file mid-run.
#
# `--interactive` is passed only when there is really a terminal.  Forcing it
# is what overrode `aq install`'s own isatty() detection (src/cli/install.py)
# and let the garbled read happen at all; with no terminal, the installer
# chooses non-interactive itself and reports needs_user rather than guessing.
#
# `aq install` exits 10 for a human-only checkpoint (a harness login, a
# Homebrew password).  That is a resumable stopping point, not a crash, so the
# closing guidance must still print: `set -e` would abort before it.
set +e
if [[ -t 0 ]]; then
    "$aq_command" install --interactive
elif (exec </dev/tty) 2>/dev/null; then
    # `[[ -r /dev/tty ]]` is not the test to use here: it reports readable even
    # where there is no controlling terminal to open, and the redirect then
    # fails and skips the installer silently.  Actually opening it is the only
    # honest probe.
    "$aq_command" install --interactive </dev/tty
else
    # No terminal at all (CI, a hook, a detached run).  `aq install` detects
    # that itself and goes non-interactive, but it must not inherit the pipe
    # either: /dev/null guarantees the rest of this script cannot be eaten.
    "$aq_command" install </dev/null
fi
status=$?
set -e

if [[ "$host" == "wsl" ]]; then
    next_action "Continue in this Ubuntu terminal. If AQ later reports a dashboard URL, open that localhost URL in your Windows browser. WSL can reach a Windows-only service at: ip route show default | awk '{print \$3}'."
else
    next_action "Open a new terminal (or run: source $profile_file) so 'aq' is on your PATH, then follow docs/tutorials/first-task.md."
fi

exit "$status"
