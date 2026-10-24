"""Doctor data model — severities, check results, check descriptors, context.

See ``docs/specs/design/trust-and-ops.md`` §5 and
``docs/specs/implementation/trust-and-ops.md`` §5.1.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.config import AppConfig


class Severity(str, Enum):
    """Result severity, ordered from healthy to broken.

    * ``OK`` — check passed; reported so the catalog stays visible.
    * ``INFO`` — intentional state worth surfacing (paused subsystems, a
      subsystem that isn't enabled).  Never fails CI.
    * ``WARN`` — degraded or heading toward a problem.
    * ``ERROR`` — the daemon cannot operate correctly, or data integrity is
      at risk.
    """

    OK = "ok"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


# Rank used for the exit-code mapping (higher = worse).
SEVERITY_RANK: dict[Severity, int] = {
    Severity.OK: 0,
    Severity.INFO: 1,
    Severity.WARN: 2,
    Severity.ERROR: 3,
}


@dataclass
class CheckResult:
    """The outcome of one check run."""

    id: str
    severity: Severity
    detail: str
    fixable: bool = False
    fix_applied: bool = False
    duration_ms: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    # Health and execution are independent. Legacy producers cannot prove absence.
    execution_status: str = "unknown"
    complete: bool = False
    coverage: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # Producer-owned full model; deliberately separate from legacy CLI data.
    observation_data: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity.value,
            "detail": self.detail,
            "fixable": self.fixable,
            "fix_applied": self.fix_applied,
            "duration_ms": self.duration_ms,
            "data": self.data,
            "execution_status": self.execution_status,
            "complete": self.complete,
            "coverage": self.coverage,
            "reasons": self.reasons,
        }


@dataclass
class DoctorCheck:
    """A registered check.

    ``id`` is namespaced (``db.wal_size``, ``plugin.<name>.<id>``).  ``fix``
    is optional and must obey the ``--fix`` safety rules (design §5.4):
    idempotent and non-destructive to primary data.
    """

    id: str
    run: Callable[["DoctorContext"], Awaitable[CheckResult]]
    fix: Callable[["DoctorContext"], Awaitable[CheckResult]] | None = None
    timeout_s: float = 5.0
    owner: str = "core"
    supported_scopes: tuple[str, ...] = ()
    read_only_audited: bool = False
    finding_schema: dict[str, Any] = field(default_factory=dict)
    identity_adapter: Callable[[CheckResult], list[dict[str, Any]]] | None = None
    identity_version: int = 1
    payload_version: int = 1
    completeness_contract: str | None = None
    groups: tuple[str, ...] = ()


@dataclass
class DoctorContext:
    """Everything a check is allowed to reach.

    Checks never import another subsystem's internals — they either use the
    handles here or are registered by the subsystem that owns the state
    (design §5.5).
    """

    config: "AppConfig"
    db: Any = None
    handler: Any = None  # CommandHandler — checks that reuse existing commands
    project_id: str | None = None
    reference_time: float | None = None
    read_only: bool = False
    leave_gated: bool = False  # legacy ingestion: inventory without materializing
    # A separate context is made for each concurrent probe.
    observation_reasons: list[dict[str, Any]] = field(default_factory=list)

    def incomplete(self, reason: str, *, project_id: str | None = None) -> None:
        self.observation_reasons.append({"reason": reason, "project_id": project_id})


class ObservationUnavailable(RuntimeError):
    """A producer cannot observe a required dependency (never healthy absence)."""


# ---------------------------------------------------------------------------
# Contributed-check contract
# ---------------------------------------------------------------------------

#: Check ids owned by subsystems that are not implemented yet.  Doctor ships
#: them *reserved*: when nobody has registered the id, doctor reports
#: ``info: check not registered (subsystem not enabled)`` rather than silently
#: omitting it, so the catalog stays honest.
#:
#: Registration contract for the owning workstream:
#:
#: 1. Build a :class:`DoctorCheck` whose ``id`` is exactly the reserved string
#:    and whose ``owner`` names the workstream (``"session-runtime"``,
#:    ``"worktree-execution"``).
#: 2. Register it on the daemon-wide :class:`~src.doctor.runner.DoctorRegistry`
#:    at startup — ``orchestrator.doctor_registry.register(check)`` for core
#:    subsystems, ``PluginContext.register_doctor_check()`` for plugins.
#:    Registering a reserved id replaces the placeholder; registering any
#:    other duplicate id raises.
#: 3. Honour the ``--fix`` safety rules for anything it declares fixable
#:    (``worktrees.orphans`` may only run ``git worktree prune``; it must never
#:    delete a directory).
RESERVED_CHECK_IDS: dict[str, str] = {
    "sessions.stale": "session-runtime",
    "tmux.server": "session-runtime",
    "worktrees.orphans": "worktree-execution",
    "leases.stale": "worktree-execution",
}


# Nested read handlers can report a caught failure without changing legacy results.
observation_context: ContextVar[DoctorContext | None] = ContextVar(
    "doctor_observation_context", default=None,
)


def observation_read_failed(reason: str, project_id: str | None = None) -> None:
    ctx = observation_context.get()
    if ctx is not None:
        ctx.incomplete(reason, project_id=project_id)
