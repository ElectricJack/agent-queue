#!/usr/bin/env bash
# One-time root step for resource-gating layer 3 (hard per-session limits).
#
# Layers 1 and 2 — per-session env caps and the `aq test` semaphore — are
# cooperative: they work because every process the daemon launches inherits
# them.  A runaway that ignores them (a shell script that hardcodes `-n 24`,
# a compiler that spawns per-core) still gets to take the box down.  Layer 3
# closes that hole with a cgroup v2 scope per session, which the kernel
# enforces whether or not the process cooperates.
#
# Creating a scope with its own CPU/memory controllers is privileged unless
# the user's slice has `Delegate=yes`.  That property can only be set by
# root, and only once — hence this script.
#
# Usage:
#   sudo scripts/setup-cgroup-delegation.sh [user]
#
# Defaults to $SUDO_USER, then $USER.  Idempotent: re-running it is a no-op
# beyond re-asserting the same property.

set -euo pipefail

TARGET_USER="${1:-${SUDO_USER:-${USER:-}}}"
if [[ -z "$TARGET_USER" ]]; then
    echo "error: could not determine the target user; pass it explicitly" >&2
    exit 2
fi

if [[ "$(id -u)" -ne 0 ]]; then
    echo "error: must run as root (try: sudo $0 $TARGET_USER)" >&2
    exit 2
fi

TARGET_UID="$(id -u "$TARGET_USER")"
SLICE="user-${TARGET_UID}.slice"

echo "==> Target: ${TARGET_USER} (uid ${TARGET_UID}), slice ${SLICE}"

# --- 1. cgroup v2 must be the unified hierarchy -----------------------------
if [[ ! -f /sys/fs/cgroup/cgroup.controllers ]]; then
    cat >&2 <<'MSG'
error: cgroup v2 is not mounted as the unified hierarchy.

  Add `systemd.unified_cgroup_hierarchy=1` to the kernel command line and
  reboot, or on WSL2 set `kernelCommandLine` in .wslconfig.  Without cgroup
  v2 there is nothing to delegate; agent-queue will keep running with
  resource-gating layers 1 and 2 only.
MSG
    exit 1
fi

# --- 2. A user manager must exist for that uid ------------------------------
if ! systemctl is-active --quiet "user@${TARGET_UID}.service"; then
    echo "==> user@${TARGET_UID}.service is not active; enabling lingering"
    loginctl enable-linger "$TARGET_USER"
    systemctl start "user@${TARGET_UID}.service" || true
fi

# --- 3. Delegate the controllers agent-queue actually limits ----------------
echo "==> Setting Delegate=yes on ${SLICE}"
systemctl set-property "$SLICE" Delegate=yes

# `set-property` is persistent (it writes a drop-in), but the running slice
# needs the controllers enabled now for the current session to benefit.
systemctl daemon-reload

# --- 4. Verify as the target user -------------------------------------------
echo "==> Verifying (running a throwaway scope as ${TARGET_USER})"
if sudo -u "$TARGET_USER" \
        XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
        systemd-run --user --scope --quiet -p CPUQuota=100% -p MemoryMax=256M -- true
then
    cat <<MSG

Delegation is working.  Enable layer 3 in ~/.agent-queue/config.yaml:

  resources:
    cgroups:
      enabled: true
      cpu_quota_percent: 600   # six cores per session
      memory_max: 6G

Then restart the daemon (./run.sh restart) and confirm with:

  aq doctor --check resources.cgroups
MSG
else
    cat >&2 <<MSG

warning: the verification scope still failed.

  Delegate=yes is set, but the user manager may need a fresh login to pick
  it up.  Log the user out and back in (or reboot) and re-run:

    sudo -u ${TARGET_USER} systemd-run --user --scope -p CPUQuota=100% -- true

  agent-queue degrades to resource-gating layers 1 and 2 until this works;
  \`aq doctor --check resources.cgroups\` reports the current state.
MSG
    exit 1
fi
