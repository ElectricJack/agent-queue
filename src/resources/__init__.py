"""Resource gating — keep N concurrent agents from saturating one box.

Three independent layers, described in ``docs/guides/resource-gating.md``:

1. :mod:`src.resources.limits` — per-session env caps (xdist/BLAS/libuv
   thread counts) and ``nice``, applied by the session launcher.
2. :mod:`src.resources.box_lock` — versioned box-wide ``flock`` admission,
   using :mod:`src.resources.semaphore` for weighted capacity. ``aq test``
   takes shared admission; operator database cleanup takes exclusive admission.
3. :mod:`src.resources.limits` again — optional cgroup v2 scopes with hard
   ``CPUQuota``/``MemoryMax``, which need a one-time root step and degrade
   to layer 1 when the daemon's user has no delegation.

:mod:`src.resources.procs` attributes observed load back to the sessions
causing it, which is what makes the doctor checks actionable.
"""

from src.resources.limits import (
    CgroupDelegation,
    ResourceBudget,
    cgroup_delegation,
    resolve_budget,
    session_env_caps,
    wrap_session_argv,
)
from src.resources.box_lock import BoxLock, IncompatibleLockClient
from src.resources.procs import (
    ProcInfo,
    load_average,
    pytest_processes,
    scan_processes,
    summarize_by_session,
)
from src.resources.semaphore import (
    SlotSemaphore,
    SlotTimeout,
    default_lock_dir,
    full_suite_lock_dir,
)

__all__ = [
    "BoxLock",
    "CgroupDelegation",
    "IncompatibleLockClient",
    "ProcInfo",
    "ResourceBudget",
    "SlotTimeout",
    "SlotSemaphore",
    "cgroup_delegation",
    "default_lock_dir",
    "full_suite_lock_dir",
    "load_average",
    "pytest_processes",
    "resolve_budget",
    "scan_processes",
    "session_env_caps",
    "summarize_by_session",
    "wrap_session_argv",
]
