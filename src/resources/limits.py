"""Per-session resource caps: env derivation, ``nice``, cgroup scopes.

Layer 1 and layer 3 of resource gating (layer 2 — the global test
semaphore — lives in :mod:`src.resources.semaphore`).

The failure this prevents is concrete.  Eight agents on a 24-core box each
running ``pytest -n auto`` is up to 192 test processes; observed on
2026-09-01 as load 60+, memory pressure and SIGKILLed sessions.  ``-n auto``
asks the *machine* how many cores there are, and every agent gets the same
answer, so the only fix that scales is to tell each session a smaller truth
at launch.

Everything here is a pure function of :class:`~src.config.ResourcesConfig`
plus (for cgroups) one cached probe of the host, so the launcher stays
testable and a misconfiguration degrades instead of failing a launch.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = [
    "ResourceBudget",
    "resolve_budget",
    "session_env_caps",
    "wrap_session_argv",
    "cgroup_delegation",
    "CgroupDelegation",
    "THREAD_CAP_KEYS",
    "XDIST_WORKERS_KEY",
]

#: xdist reads this instead of counting cores when ``-n auto`` is used.
XDIST_WORKERS_KEY = "PYTEST_XDIST_AUTO_NUM_WORKERS"

#: Every library-level "how many threads may I spawn?" knob we know about.
#: BLAS backends in particular default to *one thread per core* per process,
#: which is how a handful of numpy imports becomes a load spike.
THREAD_CAP_KEYS: tuple[str, ...] = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    # libuv's blocking-work pool — Node (and therefore the Claude CLI)
    # sizes filesystem/DNS concurrency from it.
    "UV_THREADPOOL_SIZE",
)


@dataclass(frozen=True)
class ResourceBudget:
    """What one session is allowed to assume about the box.

    ``cores`` is the whole machine; ``cpu_share`` is this session's slice.
    The difference matters for messages: an agent that is told "4 workers"
    should be able to see that the box has 24 and that 8 agents are sharing
    it, or the cap looks arbitrary.
    """

    cores: int
    cpu_share: int
    max_concurrent_agents: int
    test_slots: int
    test_workers: int
    nice: int


def _resources(config) -> "object | None":
    """The ``resources`` section, or ``None`` when the config predates it.

    Test doubles and older configs hand us objects without the section;
    a missing section means "no gating", never a crash at launch.
    """
    return getattr(config, "resources", None)


def resolve_budget(config) -> ResourceBudget | None:
    """The effective budget, or ``None`` when gating is off/unavailable."""
    res = _resources(config)
    if res is None or not getattr(res, "enabled", False):
        return None
    return ResourceBudget(
        cores=res.core_count(),
        cpu_share=res.cpu_share(),
        max_concurrent_agents=max(1, int(res.max_concurrent_agents or 1)),
        test_slots=max(1, int(res.test_slots or 1)),
        test_workers=res.test_worker_cap(),
        nice=int(res.session_nice or 0),
    )


def session_env_caps(config, *, skip: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment caps to add to a session launch.

    ``skip`` is the harness's own ``env`` map.  A key an operator wrote in a
    harness file is deliberate and wins: this returns caps only for keys the
    operator did *not* pin, which is what keeps the vault stopgap
    (``PYTEST_XDIST_AUTO_NUM_WORKERS: "4"``) authoritative on a box whose
    operator still wants it there.

    ``AQ_CPU_SHARE`` / ``AQ_TEST_SLOTS`` / ``AQ_TEST_WORKERS`` ride along so
    ``aq test`` inside the session sees the same numbers the daemon derived,
    without re-reading a config file that may not be visible from a
    worktree.
    """
    budget = resolve_budget(config)
    if budget is None:
        return {}
    pinned = set(skip or ())
    share = str(budget.cpu_share)
    caps: dict[str, str] = {XDIST_WORKERS_KEY: share}
    for key in THREAD_CAP_KEYS:
        caps[key] = share
    caps["AQ_CPU_SHARE"] = share
    caps["AQ_CPU_CORES"] = str(budget.cores)
    caps["AQ_TEST_SLOTS"] = str(budget.test_slots)
    caps["AQ_TEST_WORKERS"] = str(budget.test_workers)
    return {k: v for k, v in caps.items() if k not in pinned}


# ---------------------------------------------------------------------------
# cgroup v2 scopes (layer 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CgroupDelegation:
    """Whether this user may create a resource-limited systemd scope.

    ``available`` is the only field callers branch on; ``reason`` exists so
    doctor and the one-time log line can say *why* rather than just "no".
    """

    available: bool
    reason: str


def _probe_delegation() -> CgroupDelegation:
    """Ask the host, once, whether ``systemd-run --user --scope`` works.

    The probe actually runs a trivial scope with the same controller
    properties a session would use, because every cheaper signal lies:
    ``systemd-run`` exists on boxes without a user manager, a user manager
    exists without ``Delegate=yes``, and cgroup v2 is mounted on hosts where
    the controllers are not delegated.  The probe is ~50 ms and cached for
    the life of the process.
    """
    if not shutil.which("systemd-run"):
        return CgroupDelegation(False, "systemd-run is not installed")
    if not os.path.isdir("/sys/fs/cgroup"):
        return CgroupDelegation(False, "cgroup filesystem is not mounted")
    try:
        proc = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "-p",
                "CPUQuota=100%",
                "--",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CgroupDelegation(False, f"systemd-run probe failed: {exc}")
    if proc.returncode == 0:
        return CgroupDelegation(True, "systemd-run --user --scope accepted CPUQuota")
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return CgroupDelegation(
        False,
        detail[-1] if detail else f"systemd-run exited {proc.returncode}",
    )


@functools.lru_cache(maxsize=1)
def cgroup_delegation() -> CgroupDelegation:
    """Cached :func:`_probe_delegation`.  Call ``cache_clear()`` in tests."""
    result = _probe_delegation()
    if not result.available:
        logger.info(
            "cgroup delegation unavailable (%s) — falling back to env caps + nice. "
            "Run scripts/setup-cgroup-delegation.sh once as root to enable hard limits.",
            result.reason,
        )
    return result


def _cgroup_prefix(config, *, scope_name: str | None) -> list[str]:
    res = _resources(config)
    cg = getattr(res, "cgroups", None) if res is not None else None
    if cg is None or not getattr(cg, "enabled", False):
        return []
    if not cgroup_delegation().available:
        return []
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
    if scope_name:
        argv.extend(["--unit", f"aq-{scope_name}"])
    argv.extend(["-p", f"CPUQuota={int(cg.cpu_quota_percent)}%"])
    memory_max = str(getattr(cg, "memory_max", "") or "").strip()
    if memory_max:
        argv.extend(["-p", f"MemoryMax={memory_max}"])
    argv.append("--")
    return argv


def wrap_session_argv(
    argv: Sequence[str], config, *, scope_name: str | None = None
) -> list[str]:
    """Wrap a harness argv in the process-level limits that apply to it.

    Outermost first: the cgroup scope (layer 3, when enabled *and*
    delegated), then ``nice`` (layer 1).  ``nice`` is the cheap half of the
    fix and is what keeps the daemon, the dashboard and tmux schedulable
    while eight agents compile: the agents run at +10, everything that has
    to answer a human stays at 0.

    Returns ``argv`` unchanged when gating is off, so a caller can wrap
    unconditionally.
    """
    argv = list(argv)
    if not argv:
        return argv
    budget = resolve_budget(config)
    if budget is None:
        return argv
    if budget.nice and shutil.which("nice"):
        argv = ["nice", "-n", str(budget.nice), *argv]
    prefix = _cgroup_prefix(config, scope_name=scope_name)
    return [*prefix, *argv] if prefix else argv
