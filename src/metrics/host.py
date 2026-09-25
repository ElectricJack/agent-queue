"""The one host sampler: PSI, test-slot occupancy and ungated load.

Everything here is a cheap read of ``/proc`` or a lock directory, and it runs
on the sampler's slow tier.  A reading that overruns the budget is not
repeated for :data:`BACKOFF_SAMPLES` calls: a sampler that loads the box it is
measuring is worse than a stale reading (spec 2026-09-24 dashboard
performance §4.1; the planner spec's "skip costly attribution on overrun and
mark stale").  Nothing else in the daemon samples ``/proc`` on a timer.

Signals the host cannot supply are ``None`` beside a ``reason`` string, never
``0``: a zero pressure reading is a claim, an absent one is an admission.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.resources.procs import ProcInfo, pytest_processes
from src.resources.semaphore import SlotSemaphore, default_lock_dir
from src.resources.test_runs import ORPHANED, held_slots

logger = logging.getLogger(__name__)

__all__ = [
    "BACKOFF_SAMPLES",
    "PSI_DIR",
    "HostSampler",
    "read_pressure",
    "read_test_slots",
    "read_ungated_load",
]

PSI_DIR = Path("/proc/pressure")

#: Calls that return the last reading, marked stale, after one overruns the budget.
BACKOFF_SAMPLES = 5

#: Fallback for :attr:`MetricsConfig.perf_host_budget_ms` — the spec's per-sample budget.
DEFAULT_BUDGET_MS = 20.0

_RESOURCES = ("cpu", "io", "memory")

#: Most worktree slots one sample names as running ungated tests.  The count is
#: always exact; this bounds only the names, which are stored every second.
_MAX_SLOT_NAMES = 8


def _null_pressure(reason: str | None) -> dict[str, Any]:
    return {**{name: None for name in _RESOURCES}, "reason": reason}


def _null_slots(reason: str) -> dict[str, Any]:
    return {"used": None, "total": None, "waiting": None, "orphaned": None, "reason": reason}


def _null_ungated() -> dict[str, Any]:
    return {"pytest_processes": None, "unattributed": None, "unattributed_slots": []}


def _parse_pressure(text: str) -> dict[str, float | None]:
    """``some``/``full`` ``avg10`` from one PSI file.

    Kernels before 5.13 print no ``full`` line for ``cpu``; that value stays
    ``None``.  So does a field that does not parse.
    """
    entry: dict[str, float | None] = {"some_avg10": None, "full_avg10": None}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("some", "full"):
            continue
        fields = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
        try:
            entry[f"{parts[0]}_avg10"] = float(fields["avg10"])
        except (KeyError, ValueError):
            continue
    return entry


def read_pressure(root: Path = PSI_DIR) -> dict[str, Any]:
    """PSI ``avg10`` per resource: ``{"cpu", "io", "memory", "reason"}``.

    ``reason`` is ``psi_unavailable`` when the kernel has no PSI directory and
    ``psi_unreadable`` when it has one but a resource file cannot be read
    (``psi=0`` on the kernel command line fails every read with
    ``EOPNOTSUPP``); the unread resources are ``None``.
    """
    if not root.is_dir():
        return _null_pressure("psi_unavailable")
    out = _null_pressure(None)
    for name in _RESOURCES:
        try:
            out[name] = _parse_pressure((root / name).read_text(encoding="utf-8"))
        except OSError:
            out["reason"] = "psi_unreadable"
    return out


def read_test_slots(config, *, lock_dir: Path | None = None) -> dict[str, Any]:
    """``aq test`` slot occupancy: ``{"used", "total", "waiting", "orphaned", "reason"}``.

    The counts are floats because every stored gauge is averaged by the
    roll-up.  With resource gating off there are no slots to count, so every
    count is ``None`` with ``reason: "resources_disabled"``.  Reading never
    creates the lock directory.
    """
    resources = getattr(config, "resources", None)
    if resources is None or not getattr(resources, "enabled", False):
        return _null_slots("resources_disabled")
    directory = Path(lock_dir) if lock_dir is not None else default_lock_dir(config)
    snapshot = SlotSemaphore(directory, int(resources.test_slots)).snapshot()
    orphaned = sum(1 for held in held_slots(directory) if held.state == ORPHANED)
    return {
        "used": float(snapshot["total"] - snapshot["free"]),
        "total": float(snapshot["total"]),
        "waiting": float(len(snapshot["waiting"])),
        "orphaned": float(orphaned),
        "reason": None,
    }


def read_ungated_load(procs: list[ProcInfo] | None = None) -> dict[str, Any]:
    """pytest processes box-wide, and how many carry no session marker.

    A process with none of ``AQ_SESSION_ID``, ``AQ_TASK_ID`` or
    ``AQ_SESSION_NAME`` in its environment was not launched by a session — a
    human's shell or a CI runner — and so ran outside every per-session cap.
    ``AQ_SESSION_ID`` is the one every session carries; a pool worker has no
    ``AQ_TASK_ID``.  The worktree slot its ``cwd`` sits in is the only name
    an unattributed process has; paths never leave here.
    """
    procs = pytest_processes() if procs is None else procs
    unattributed = [
        p for p in procs if p.session_id is None and p.task_id is None and p.session is None
    ]
    slots = sorted({p.slot for p in unattributed if p.slot})
    return {
        "pytest_processes": len(procs),
        "unattributed": len(unattributed),
        "unattributed_slots": slots[:_MAX_SLOT_NAMES],
    }


class HostSampler:
    """One host reading per slow-tier tick, behind a cost budget.

    :meth:`sample` never raises: a reader that fails leaves its block's
    values ``None`` (the keys stay, so a stored sample always has one shape)
    and the sample's ``reason`` ``read_failed``, so a ``/proc`` hiccup cannot
    cost the fleet sample it rides in.
    """

    def __init__(
        self,
        config,
        *,
        budget_ms: float | None = None,
        clock: Callable[[], float] = time.perf_counter,
        pressure: Callable[..., dict[str, Any]] = read_pressure,
        slots: Callable[..., dict[str, Any]] = read_test_slots,
        ungated: Callable[..., dict[str, Any]] = read_ungated_load,
    ) -> None:
        self.config = config
        self._budget_ms = budget_ms
        self._clock = clock
        self._pressure, self._slots, self._ungated = pressure, slots, ungated
        self._skip = 0
        self._last: dict[str, Any] = {}

    def _budget(self) -> float:
        if self._budget_ms is not None:
            return float(self._budget_ms)
        metrics = getattr(self.config, "metrics", None)
        return float(
            getattr(metrics, "perf_host_budget_ms", DEFAULT_BUDGET_MS) or DEFAULT_BUDGET_MS
        )

    def _read(
        self,
        name: str,
        reader: Callable[..., dict[str, Any]],
        fallback: Callable[[], dict[str, Any]],
        *args: Any,
    ) -> tuple[dict[str, Any], bool]:
        try:
            return reader(*args), True
        except Exception:
            logger.debug("host sampler: %s read failed", name, exc_info=True)
            return fallback(), False

    def sample(self) -> dict[str, Any]:
        """``{"psi", "test_slots", "ungated", "host_ms", "stale", "reason"}``.

        After a reading costs more than the budget, the next
        :data:`BACKOFF_SAMPLES` calls return that reading again with
        ``stale: True, reason: "over_budget"`` instead of scanning.
        """
        if self._skip > 0 and self._last:
            self._skip -= 1
            return {**self._last, "stale": True, "reason": "over_budget"}
        started = self._clock()
        psi, psi_ok = self._read("psi", self._pressure, lambda: _null_pressure("psi_unreadable"))
        slots, slots_ok = self._read(
            "test_slots", self._slots, lambda: _null_slots("read_failed"), self.config
        )
        ungated, ungated_ok = self._read("ungated", self._ungated, _null_ungated)
        cost_ms = (self._clock() - started) * 1000.0
        out: dict[str, Any] = {
            "psi": psi,
            "test_slots": slots,
            "ungated": ungated,
            "host_ms": round(cost_ms, 3),
            "stale": False,
            "reason": None if psi_ok and slots_ok and ungated_ok else "read_failed",
        }
        if cost_ms > self._budget():
            self._skip = BACKOFF_SAMPLES
        self._last = out
        return out
