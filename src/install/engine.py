"""The installer engine: plan, consent, execute, resume.

This is the single orchestrator every platform, database and provider adapter
runs under.  It owns exactly five things and delegates everything else:

1. **Host admission.**  The supported-platform matrix is evaluated before any
   step runs, and an unsupported host returns ``unsupported_host`` with the
   observed facts and no mutation.
2. **Ordering and gating.**  Steps run in dependency order; a step gated on an
   unselected capability is recorded ``skipped`` with its reason preserved.
3. **Idempotence.**  A step that a previous run completed is *revalidated*
   through its read-only ``verify`` rather than re-executed, so a rerun cannot
   duplicate a resource.  Resource records are merged by ``(kind, id)``.
4. **Consent.**  A mutating step needs interactive consent or an explicit
   unattended approval; without one it stops as ``needs_user`` rather than
   installing something nobody asked for.
5. **Stopping and reporting.**  The run stops at the first unsatisfied step,
   names it, and reports how to recover — as a human summary and as one JSON
   object with a documented exit code.

The engine reads no clock of its own (``clock`` is injected), performs no
network I/O, and writes exactly one file: the resume record.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .platform import SupportVerdict, describe_host
from .results import (
    InstallOutcome,
    InstallResult,
    PlanAction,
    PlannedStep,
    ResourceRecord,
    StepResult,
    StepState,
)
from .state import (
    IncompatibleStateError,
    InstallState,
    StateError,
    assert_compatible,
    default_state_path,
    load_state,
    save_state,
)
from .steps import InstallPlanError, StepContext, StepRegistry, StepSpec, merge_resources


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


#: The consent callback's contract: given a step and its prompt, return True to
#: allow the mutation.  Interactive front-ends bind this to a terminal prompt;
#: ``--yes`` binds it to a constant.
ConsentCallback = Callable[[StepSpec], bool]

#: Progress is reported as it happens rather than accumulated, so a long
#: install shows movement instead of a silent terminal.
ProgressCallback = Callable[["ProgressEvent"], None]


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One movement in the run: a step started, or a step reached a state."""

    index: int
    total: int
    step_id: str
    title: str
    phase: str  # "start" | "done"
    state: StepState | None = None
    action: PlanAction | None = None


@dataclass(frozen=True, slots=True)
class InstallOptions:
    """Everything that changes what a run does.

    ``interactive`` and ``non_interactive`` are one flag, not two modes with
    separate code paths: unattended runs use the same engine and the same
    adapters, and differ only in where consent comes from.
    """

    installer_version: str = "0.0.0"
    target_version: str = "0.0.0"
    interactive: bool = True
    dry_run: bool = False
    resume: bool = True
    fresh: bool = False
    restart_from: str | None = None
    capabilities: frozenset[str] = frozenset()
    #: Step ids (or ``"*"``) whose mutations the caller approved up front.
    #: This is how an unattended run authorises a package install.
    approve: frozenset[str] = frozenset()
    settings: Mapping[str, Any] = field(default_factory=dict)
    state_path: Path | None = None

    @property
    def approves_all(self) -> bool:
        return "*" in self.approve

    def approves(self, step_id: str) -> bool:
        return self.approves_all or step_id in self.approve


class InstallEngine:
    """Runs a :class:`StepRegistry` under an :class:`InstallOptions`."""

    def __init__(
        self,
        registry: StepRegistry,
        options: InstallOptions | None = None,
        *,
        support: SupportVerdict | None = None,
        consent: ConsentCallback | None = None,
        progress: ProgressCallback | None = None,
        clock: Callable[[], str] = _utc_now,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self.options = options or InstallOptions()
        self._support = support
        self._consent = consent
        self._progress = progress
        self._clock = clock
        self._timer = timer

    # -- host ---------------------------------------------------------------
    @property
    def support(self) -> SupportVerdict:
        if self._support is None:
            self._support = describe_host()
        return self._support

    # -- planning -----------------------------------------------------------
    def plan(self, state: InstallState | None = None) -> tuple[PlannedStep, ...]:
        """Return what this run intends to do, without doing any of it.

        The plan is derived from the same predicates the run uses, so
        ``--dry-run`` is a report of the real decision and not a parallel
        description of it.
        """
        rows: list[PlannedStep] = []
        satisfied: set[str] = set()
        for step in self.registry.ordered():
            action, reason = self._plan_step(step, state, satisfied)
            if action in (
                PlanAction.SKIP_COMPLETED,
                PlanAction.SKIP_NOT_SELECTED,
                PlanAction.RUN,
                PlanAction.REVALIDATE,
                PlanAction.WOULD_RUN,
            ):
                satisfied.add(step.id)
            rows.append(
                PlannedStep(
                    step_id=step.id,
                    title=step.title,
                    action=action,
                    reason=reason,
                    mutating=step.mutating,
                    capability=step.capability,
                )
            )
        return tuple(rows)

    def _plan_step(
        self, step: StepSpec, state: InstallState | None, satisfied: set[str]
    ) -> tuple[PlanAction, str]:
        missing = [dependency for dependency in step.depends_on if dependency not in satisfied]
        if missing:
            return PlanAction.BLOCKED, f"depends on unsatisfied step(s): {', '.join(missing)}"
        if step.capability and step.capability not in self.options.capabilities:
            return (
                PlanAction.SKIP_NOT_SELECTED,
                f"capability '{step.capability}' was not selected",
            )
        record = state.record_for(step.id) if state else None
        if record and record.satisfied:
            if record.state is StepState.SKIPPED:
                return PlanAction.SKIP_COMPLETED, f"previously skipped: {record.summary}"
            if step.verify is None:
                return PlanAction.SKIP_COMPLETED, "completed by an earlier run"
            return PlanAction.REVALIDATE, "completed by an earlier run; revalidating"
        if step.mutating and self.options.dry_run:
            return PlanAction.WOULD_RUN, "mutating step; not executed in a dry run"
        return PlanAction.RUN, "not yet satisfied"

    # -- execution ----------------------------------------------------------
    def run(self) -> InstallResult:
        """Execute the plan and return one machine-readable result."""
        options = self.options
        support = self.support
        state_path = options.state_path or default_state_path()

        if not support.installable:
            return self._result(
                InstallOutcome.UNSUPPORTED_HOST,
                plan=(),
                steps=(),
                resources=(),
                state_path=None,
                next_action=support.remediation,
                messages=support.reasons,
            )

        try:
            state = self._load_state(state_path)
        except IncompatibleStateError as error:
            return self._result(
                InstallOutcome.INVALID_INPUT,
                plan=(),
                steps=(),
                resources=(),
                state_path=str(state_path),
                next_action=str(error),
                messages=(str(error),),
            )
        except StateError as error:
            return self._result(
                InstallOutcome.INVALID_INPUT,
                plan=(),
                steps=(),
                resources=(),
                state_path=str(state_path),
                next_action=(
                    "Inspect the file named above, then rerun with --fresh to start a new "
                    "record (the unreadable one is left in place)."
                ),
                messages=(str(error),),
            )

        try:
            invalidated = self._apply_restart(state)
            ordered = self.registry.ordered()
        except InstallPlanError as error:
            return self._result(
                InstallOutcome.INVALID_INPUT,
                plan=(),
                steps=(),
                resources=(),
                state_path=str(state_path),
                next_action=str(error),
                messages=(str(error),),
            )

        state.set_platform(support.facts)
        state.capabilities = sorted(options.capabilities)
        plan_rows = {row.step_id: row for row in self.plan(state)}

        results: list[StepResult] = []
        resources: tuple[ResourceRecord, ...] = tuple(state.resources.values())
        satisfied: set[str] = set()
        stopped = False
        messages: list[str] = [f"invalidated by --restart-from: {name}" for name in invalidated]

        for index, step in enumerate(ordered, start=1):
            row = plan_rows[step.id]
            if stopped:
                results.append(
                    StepResult.skipped(step.id, "not reached: an earlier step stopped the run")
                )
                continue

            self._emit(
                ProgressEvent(index, len(ordered), step.id, step.title, "start", action=row.action)
            )
            started = self._timer()
            result = self._execute(step, row, satisfied, support, resources, state)
            result = result.with_duration(int((self._timer() - started) * 1000))
            results.append(result)
            self._emit(
                ProgressEvent(
                    index,
                    len(ordered),
                    step.id,
                    step.title,
                    "done",
                    state=result.state,
                    action=row.action,
                )
            )

            if result.resources:
                resources = merge_resources(resources, result.resources)
            if result.satisfied:
                satisfied.add(step.id)
            else:
                stopped = True

            if not options.dry_run and row.action is not PlanAction.WOULD_RUN:
                state.apply(
                    result,
                    now=self._clock(),
                    input_schema_version=step.input_schema_version,
                )

        outcome = self._classify(results)
        saved_path: str | None = None
        if not options.dry_run:
            save_state(state, state_path, now=self._clock())
            saved_path = str(state_path)

        return self._result(
            outcome,
            plan=tuple(plan_rows.values()),
            steps=tuple(results),
            resources=resources,
            state_path=saved_path,
            next_action=self._next_action(outcome, results),
            messages=tuple(messages),
        )

    # -- internals ----------------------------------------------------------
    def _load_state(self, state_path: Path) -> InstallState:
        options = self.options
        fresh = InstallState(
            installer_version=options.installer_version,
            target_version=options.target_version,
        )
        if options.fresh or not options.resume:
            return fresh
        state = load_state(state_path)
        if state is None:
            return fresh
        assert_compatible(state, installer_version=options.installer_version)
        state.target_version = options.target_version
        return state

    def _apply_restart(self, state: InstallState) -> tuple[str, ...]:
        if not self.options.restart_from:
            return ()
        targets = self.registry.dependents_of(self.options.restart_from)
        if self.options.dry_run:
            # A dry run must not change the durable record, but the plan it
            # prints has to reflect the restart the caller asked for.
            for step_id in targets:
                state.steps.pop(step_id, None)
            return targets
        return state.invalidate(targets, now=self._clock())

    def _execute(
        self,
        step: StepSpec,
        row: PlannedStep,
        satisfied: set[str],
        support: SupportVerdict,
        resources: Sequence[ResourceRecord],
        state: InstallState,
    ) -> StepResult:
        if row.action is PlanAction.BLOCKED:
            return StepResult.failed(
                step.id,
                row.reason,
                "Resolve the dependency named above, then rerun `aq install`.",
                retryable=True,
            )
        if row.action is PlanAction.SKIP_NOT_SELECTED:
            return StepResult.skipped(step.id, row.reason)
        if row.action is PlanAction.SKIP_COMPLETED:
            # Re-report the state the record holds rather than flattening a
            # previous success into "skipped": a step with no verifier is not
            # re-executed, but the record must not decay on every rerun.
            record = state.record_for(step.id)
            summary = record.summary if record and record.summary else row.reason
            if record and record.state is StepState.SUCCEEDED:
                return StepResult.succeeded(step.id, summary, detail={"carried_forward": True})
            return StepResult.skipped(step.id, summary, detail={"carried_forward": True})
        if row.action is PlanAction.WOULD_RUN:
            return StepResult.skipped(step.id, row.reason, detail={"dry_run": True})

        context = StepContext(
            support=support,
            options=dict(self.options.settings),
            dry_run=self.options.dry_run,
            interactive=self.options.interactive,
            resources={record.key: record for record in resources},
            completed=dict(state.completed_states()),
        )

        if row.action is PlanAction.REVALIDATE:
            verified = self._verify(step, context)
            if verified is True:
                return StepResult.succeeded(
                    step.id,
                    "already satisfied; verified without repeating the step",
                    detail={"revalidated": True},
                )
            if isinstance(verified, StepResult):
                return verified
            # The observable condition is gone (a package was removed, a
            # directory deleted).  Fall through and run the step again.

        if step.mutating:
            denial = self._check_consent(step)
            if denial is not None:
                return denial

        return self._invoke(step, context)

    def _verify(self, step: StepSpec, context: StepContext) -> bool | StepResult:
        if step.verify is None:
            return True
        try:
            return bool(step.verify(context))
        except Exception as error:  # noqa: BLE001 - a broken verifier is not success
            return StepResult.failed(
                step.id,
                f"verification of the completed step raised {type(error).__name__}: {error}",
                f"Rerun with `--restart-from {step.id}` to redo this step and its dependents.",
            )

    def _check_consent(self, step: StepSpec) -> StepResult | None:
        options = self.options
        if options.approves(step.id):
            return None
        if not options.interactive:
            return StepResult.needs_user(
                step.id,
                f"unattended run has no approval for the mutating step '{step.id}'",
                (
                    f"Rerun with `--approve {step.id}` (or `--yes` to approve every mutating "
                    "step), or list the step under `approve:` in the install input file."
                ),
                detail={"consent_prompt": step.consent_prompt},
            )
        if self._consent is None or not self._consent(step):
            return StepResult.skipped(
                step.id,
                "declined by the operator",
                detail={"consent_prompt": step.consent_prompt},
            )
        return None

    def _invoke(self, step: StepSpec, context: StepContext) -> StepResult:
        try:
            result = step.run(context)
        except Exception as error:  # noqa: BLE001 - an adapter's crash is this step's failure
            return StepResult.failed(
                step.id,
                f"{type(error).__name__}: {error}",
                (
                    f"The step '{step.id}' raised. Fix the condition it reports and rerun "
                    f"`aq install`; use `--restart-from {step.id}` to force it to re-execute."
                ),
            )
        if not isinstance(result, StepResult):
            return StepResult.failed(
                step.id,
                f"step returned {type(result).__name__}, not a StepResult",
                f"This is a bug in the adapter that owns '{step.id}' ({step.owner}).",
                retryable=False,
            )
        if result.step_id != step.id:
            return StepResult.failed(
                step.id,
                f"step reported results for '{result.step_id}'",
                f"This is a bug in the adapter that owns '{step.id}' ({step.owner}).",
                retryable=False,
            )
        return result

    def _classify(self, results: Iterable[StepResult]) -> InstallOutcome:
        outcome = InstallOutcome.READY
        for result in results:
            if result.state is StepState.FAILED:
                return InstallOutcome.FAILED
            if result.state is StepState.NEEDS_USER:
                outcome = InstallOutcome.NEEDS_USER
        return outcome

    def _next_action(self, outcome: InstallOutcome, results: Sequence[StepResult]) -> str | None:
        if outcome is InstallOutcome.READY:
            return None
        for result in results:
            if result.state in (StepState.FAILED, StepState.NEEDS_USER) and result.remediation:
                return result.remediation
        return None

    def _emit(self, event: ProgressEvent) -> None:
        if self._progress is not None:
            self._progress(event)

    def _result(
        self,
        outcome: InstallOutcome,
        *,
        plan: tuple[PlannedStep, ...],
        steps: tuple[StepResult, ...],
        resources: tuple[ResourceRecord, ...],
        state_path: str | None,
        next_action: str | None,
        messages: tuple[str, ...],
    ) -> InstallResult:
        return InstallResult(
            outcome=outcome,
            installer_version=self.options.installer_version,
            target_version=self.options.target_version,
            dry_run=self.options.dry_run,
            interactive=self.options.interactive,
            platform=self.support.to_dict(),
            capabilities=tuple(sorted(self.options.capabilities)),
            plan=plan,
            steps=steps,
            resources=resources,
            state_path=state_path,
            next_action=next_action,
            messages=messages,
        )


def run_install(
    registry: StepRegistry,
    options: InstallOptions | None = None,
    **kwargs: Any,
) -> InstallResult:
    """Convenience wrapper around :class:`InstallEngine`."""
    return InstallEngine(registry, options, **kwargs).run()


__all__ = [
    "ConsentCallback",
    "InstallEngine",
    "InstallOptions",
    "ProgressCallback",
    "ProgressEvent",
    "run_install",
]
