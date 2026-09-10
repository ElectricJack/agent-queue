"""Result vocabulary for the installer engine.

The engine, every platform/provider adapter and the ``aq install`` CLI share
exactly one set of terminal step states, one outcome classification and one
exit-code table.  ``docs/plans/install-onboarding/contract.md`` ("Installer
protocol") is the contract these types implement; the user-facing
documentation of the JSON shape and the exit codes is
``docs/reference/cli/install.md``.

Nothing in this module performs I/O or reads a clock, so a caller can build a
result in a test without a machine to install onto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: Bumped when the ``aq install --json`` payload changes shape incompatibly.
RESULT_SCHEMA_VERSION = 1


class StepState(str, Enum):
    """The four terminal states a step may report (contract, "Installer protocol")."""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    NEEDS_USER = "needs_user"
    FAILED = "failed"


#: States that satisfy a dependent step's precondition.  A ``skipped`` optional
#: capability must not block the rest of the install, which is why it is here.
SATISFIED_STATES: frozenset[StepState] = frozenset({StepState.SUCCEEDED, StepState.SKIPPED})


class InstallOutcome(str, Enum):
    """The run-level classification reported to a human and to automation."""

    READY = "ready"
    NEEDS_USER = "needs_user"
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_HOST = "unsupported_host"
    FAILED = "failed"


#: Stable process exit codes.  These are API: scripts branch on them, so a
#: value may be added but never reassigned.  ``docs/reference/cli/install.md``
#: is the published copy of this table.
EXIT_CODES: dict[InstallOutcome, int] = {
    InstallOutcome.READY: 0,
    InstallOutcome.NEEDS_USER: 10,
    InstallOutcome.INVALID_INPUT: 11,
    InstallOutcome.UNSUPPORTED_HOST: 12,
    InstallOutcome.FAILED: 20,
}


def exit_code(outcome: InstallOutcome) -> int:
    """Return the documented process exit code for *outcome*."""
    return EXIT_CODES[outcome]


class PlanAction(str, Enum):
    """What the engine intends to do with a step on this run.

    ``WOULD_RUN`` is the dry-run action for a *mutating* step: the plan names
    it, and the engine deliberately does not execute it.
    """

    RUN = "run"
    REVALIDATE = "revalidate"
    WOULD_RUN = "would_run"
    SKIP_NOT_SELECTED = "skip_not_selected"
    SKIP_COMPLETED = "skip_completed"
    BLOCKED = "blocked"
    NOT_REACHED = "not_reached"


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    """One thing the installer created, or found and reused.

    ``owned`` is the uninstall/repair boundary: a resource the installer
    created is owned and may be removed by an explicit destructive request; a
    reused pre-existing resource never is.  Identity is ``(kind, id)`` so a
    rerun records the same resource once rather than twice.
    """

    kind: str
    id: str
    owned: bool = True
    reused: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "owned": self.owned,
            "reused": self.reused,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ResourceRecord:
        return cls(
            kind=str(payload["kind"]),
            id=str(payload["id"]),
            owned=bool(payload.get("owned", True)),
            reused=bool(payload.get("reused", False)),
            detail=dict(payload.get("detail") or {}),
        )


@dataclass(frozen=True, slots=True)
class StepResult:
    """The outcome of one step.

    ``remediation`` is required for anything a human has to act on: the
    acceptance criterion for this engine is that a failure names the exact
    step *and* what to do about it, so :meth:`failed` and :meth:`needs_user`
    take it as a positional argument rather than leaving it optional.
    """

    step_id: str
    state: StepState
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    remediation: str | None = None
    retryable: bool = True
    resources: tuple[ResourceRecord, ...] = ()
    duration_ms: int | None = None

    @property
    def satisfied(self) -> bool:
        return self.state in SATISFIED_STATES

    def with_duration(self, duration_ms: int) -> StepResult:
        return replace_result(self, duration_ms=duration_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "state": self.state.value,
            "summary": self.summary,
            "detail": dict(self.detail),
            "remediation": self.remediation,
            "retryable": self.retryable,
            "resources": [resource.to_dict() for resource in self.resources],
            "duration_ms": self.duration_ms,
        }

    # -- constructors ------------------------------------------------------
    @classmethod
    def succeeded(
        cls,
        step_id: str,
        summary: str,
        *,
        detail: dict[str, Any] | None = None,
        resources: tuple[ResourceRecord, ...] = (),
    ) -> StepResult:
        return cls(
            step_id=step_id,
            state=StepState.SUCCEEDED,
            summary=summary,
            detail=detail or {},
            resources=resources,
        )

    @classmethod
    def skipped(
        cls, step_id: str, reason: str, *, detail: dict[str, Any] | None = None
    ) -> StepResult:
        return cls(
            step_id=step_id,
            state=StepState.SKIPPED,
            summary=reason,
            detail=detail or {},
        )

    @classmethod
    def needs_user(
        cls,
        step_id: str,
        summary: str,
        remediation: str,
        *,
        detail: dict[str, Any] | None = None,
        resources: tuple[ResourceRecord, ...] = (),
    ) -> StepResult:
        return cls(
            step_id=step_id,
            state=StepState.NEEDS_USER,
            summary=summary,
            detail=detail or {},
            remediation=remediation,
            retryable=True,
            resources=resources,
        )

    @classmethod
    def failed(
        cls,
        step_id: str,
        summary: str,
        remediation: str,
        *,
        detail: dict[str, Any] | None = None,
        retryable: bool = True,
        resources: tuple[ResourceRecord, ...] = (),
    ) -> StepResult:
        return cls(
            step_id=step_id,
            state=StepState.FAILED,
            summary=summary,
            detail=detail or {},
            remediation=remediation,
            retryable=retryable,
            resources=resources,
        )


def replace_result(result: StepResult, **changes: Any) -> StepResult:
    """``dataclasses.replace`` for a slotted frozen dataclass."""
    from dataclasses import replace

    return replace(result, **changes)


@dataclass(frozen=True, slots=True)
class PlannedStep:
    """One row of the execution plan, with the reason for its action."""

    step_id: str
    title: str
    action: PlanAction
    reason: str
    mutating: bool = False
    capability: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "action": self.action.value,
            "reason": self.reason,
            "mutating": self.mutating,
            "capability": self.capability,
        }


@dataclass(frozen=True, slots=True)
class InstallResult:
    """The complete machine-readable result of one ``aq install`` run."""

    outcome: InstallOutcome
    installer_version: str
    target_version: str
    dry_run: bool
    interactive: bool
    platform: dict[str, Any]
    capabilities: tuple[str, ...]
    plan: tuple[PlannedStep, ...]
    steps: tuple[StepResult, ...]
    resources: tuple[ResourceRecord, ...] = ()
    state_path: str | None = None
    next_action: str | None = None
    messages: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return exit_code(self.outcome)

    @property
    def blocking_step(self) -> StepResult | None:
        """The first step that stopped the run, if any."""
        for step in self.steps:
            if step.state in (StepState.FAILED, StepState.NEEDS_USER):
                return step
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "installer_version": self.installer_version,
            "target_version": self.target_version,
            "dry_run": self.dry_run,
            "interactive": self.interactive,
            "platform": dict(self.platform),
            "capabilities": list(self.capabilities),
            "plan": [row.to_dict() for row in self.plan],
            "steps": [step.to_dict() for step in self.steps],
            "resources": [resource.to_dict() for resource in self.resources],
            "state_path": self.state_path,
            "blocking_step": self.blocking_step.step_id if self.blocking_step else None,
            "next_action": self.next_action,
            "messages": list(self.messages),
        }
