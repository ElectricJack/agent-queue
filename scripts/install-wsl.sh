#!/usr/bin/env bash
# Compatibility shim for the previous WSL-only bootstrap URL.
#
# The WSL2 and macOS bootstraps are now one script, scripts/install.sh, which
# detects the host and takes the right branch.  This file stays so that a
# bookmarked URL, an older scripts/install-windows.ps1, or a pinned command
# keeps working; it adds no behaviour of its own.
set -euo pipefail

printf 'scripts/install-wsl.sh is now scripts/install.sh; running it.\n' >&2
curl -fsSL https://raw.githubusercontent.com/ElectricJack/agent-queue/main/scripts/install.sh \
    | bash -s -- "$@"
