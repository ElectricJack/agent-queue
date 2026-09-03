"""The one Playbook V2 execution engine — Package 4 child plan §3.4 and §3.5.

Replaces three divergent execution paths (``pipeline_runner.py``,
``runner.py``, and assignment routing's private runner) with a single walk
over the strict V2 artifact.  What makes it one engine rather than three is
that *mode selects executor implementations, not a different graph*: live,
dry-run and shadow traverse the same steps, resolve the same references and
take the same transitions, and only the executor table changes.

Four V1 behaviours it deliberately does not inherit, each with a test:

* one run row per matching *rule*, not one per event (``core.py:944-957``);
* a business outcome with no edge fails the run rather than completing it
  (``pipeline_runner.py:151-158``);
* a cancellation is durable state the run reads, not an advisory row a live
  run overwrites (``playbook_commands.py:511-519``);
* an unrecognised step kind raises rather than ending the walk
  (``runner.py:2270``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from src.commands.authorization import authorize_command
from src.commands.contracts.models import OutcomeClass
from src.playbooks.artifact_ref import ArtifactRef
from src.playbooks.definition import (
    AgentTaskStep,
    CommandStep,
    ForEachStep,
    PlaybookDefinition,
    Rule,
    WaitStep,
    step_targets,
)
from src.playbooks.executors import executor_for
from src.playbooks.executors.agent_task import (
    cancel_child_task,
    child_outcome_for_status,
    narrow_for_child,
    resolve_profile_policy,
)
from src.playbooks.executors.base import (
    ENGINE_RESERVED_OUTCOMES,
    GOTO_CAPABLE_STEP_KINDS,
    EngineServices,
    ExecutionMode,
    ExecutorResult,
    StepContext,
    StepControl,
)
from src.playbooks.executors.wait import WaitResumption, resolve_wait_result
from src.playbooks.expressions import (
    ResolutionScope,
    ValueResolutionError,
    resolve_value,
)
from src.playbooks.receipts import StepReceipt, idempotency_key, transition_id
from src.playbooks.run_state import (
    DuplicateRun,
    LoopFrame,
    RunLifecycle,
    RunSnapshot,
    SnapshotVersionConflict,
    StateLimitExceeded,
    bind_step_output,
)
from src.playbooks.waits import EMPTY_WAIT_CHANGES, WaitChangeSet, WaitClaim, WaitSpec

logger = logging.getLogger(__name__)

#: A walk that visits more steps than this has a cycle the validator did not
#: catch.  It fails the run rather than spinning: an engine that never
#: returns is indistinguishable from a hung daemon.
MAX_STEP_VISITS = 1000

#: Bus events.  Package 7 reads the two run-level ones by name.
EVENT_RUN_STARTED = "playbook.v2.run.started"
EVENT_STEP_COMPLETED = "playbook.v2.step.completed"
EVENT_RUN_FINISHED = "playbook.v2.run.finished"

_TERMINAL_LIFECYCLE: dict[str, RunLifecycle] = {
    "completed": RunLifecycle.COMPLETED,
    "failed": RunLifecycle.FAILED,
    "cancelled": RunLifecycle.CANCELLED,
}


# --------------------------------------------------------------------------
# Resume causes — a closed union (§3.5)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventArrived:
    event_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TimerFired:
    wait_id: str


@dataclass(frozen=True, slots=True)
class ChildTaskCompleted:
    task_id: str
    status: str


@dataclass(frozen=True, slots=True)
class HumanDecision:
    decision: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OperatorResolution:
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)


ResumeCause = EventArrived | TimerFired | ChildTaskCompleted | HumanDecision | OperatorResolution


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingEventRef:
    playbook_id: str
    event_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """What one event produced.  Field names are locked by §3.5.

    ``rules_selected`` and the recorded commands are what Package 6's
    shadow-parity harness compares, so they are reported even when no run
    was created.
    """

    dispatch_id: str
    rules_selected: tuple[str, ...] = ()
    run_ids: tuple[str, ...] = ()
    pending: tuple[PendingEventRef, ...] = ()
    #: Rules whose run already existed for this dispatch — a replay.
    deduplicated: tuple[str, ...] = ()
    #: Ordered ``(step_id, command_name)`` pairs, for shadow parity.
    commands: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: str
    lifecycle: RunLifecycle
    outcome: str
    snapshot: RunSnapshot | None = None
    receipts: tuple[StepReceipt, ...] = ()


class InMemoryRunRecorder:
    """The repository stand-in for dry-run and shadow (§3.3.5).

    The real repository is not merely unused in those modes — it is not
    wired in at all, which is the structural half of the no-side-effect
    proof.  Its behavioural half is T-12.
    """

    def __init__(self) -> None:
        self.snapshots: dict[str, RunSnapshot] = {}
        self.receipts: list[StepReceipt] = []

    async def create_run(self, snapshot: RunSnapshot) -> RunSnapshot:
        self.snapshots[snapshot.run_id] = snapshot
        return snapshot

    async def load_run(self, run_id: str) -> RunSnapshot | None:
        return self.snapshots.get(run_id)

    async def find_run_for_dispatch(self, dispatch_id: str, rule_id: str) -> RunSnapshot | None:
        return None

    async def commit_boundary(
        self, snapshot: RunSnapshot, receipt: StepReceipt, wait_changes: Any = None
    ) -> RunSnapshot:
        stored = replace(snapshot, version=snapshot.version + 1)
        self.snapshots[snapshot.run_id] = stored
        self.receipts.append(receipt)
        return stored


@dataclass(slots=True)
class _Attempt:
    """One pass through §3.4, carried between its numbered stages."""

    snapshot: RunSnapshot
    step_id: str
    step: Any
    started_at: float
    #: Who ran it.  Carried on the attempt so the receipt records the
    #: identity that actually executed, not the engine's construction-time
    #: default — a resumed run can carry a different principal.
    principal: Any = None
    outcome: str = ""
    control: StepControl = StepControl.ADVANCE
    next_step_id: str | None = None
    lifecycle: RunLifecycle = RunLifecycle.RUNNING
    selected_transition: str | None = None
    value: Any | None = None
    receipt_inputs: Mapping[str, Any] = field(default_factory=dict)
    receipt_result: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    timed_out: bool = False
    cancelled_at: float | None = None
    idempotency_key: str = ""
    attempt: int = 1
    iteration: int = -1
    #: The suspension this boundary opens, and the wait changes that travel
    #: with it.  Both live on the attempt so ``_commit`` stays the single
    #: place that talks to the repository.
    wait: WaitSpec | None = None
    wait_changes: WaitChangeSet = EMPTY_WAIT_CHANGES
    wait_id: str | None = None
    loop_frame: LoopFrame | None = None
    clear_loop: bool = False
    #: Set by the agent-task executor.  Persisted on the snapshot *before*
    #: the run is observable as paused, so a paused run always knows what it
    #: is waiting for.
    child_task_id: str | None = None
    #: Clear this run's registered waits at the boundary — what reconciling a
    #: delivered child completion does.
    clear_waits: bool = False


class PlaybookEngine:
    """Dispatch, single-rule execution and resume over the strict artifact."""

    def __init__(
        self,
        *,
        services: EngineServices,
        runs: Any | None = None,
        waits: Any | None = None,
        activations: Any | None = None,
        max_step_visits: int = MAX_STEP_VISITS,
    ) -> None:
        self.services = services
        self.runs = runs
        self.waits = waits
        self.activations = activations
        self.max_step_visits = max_step_visits

    # ------------------------------------------------------------------
    # §4.2 — dispatch
    # ------------------------------------------------------------------

    async def dispatch_event(
        self,
        event: Mapping[str, Any],
        principal: Any,
        mode: ExecutionMode = ExecutionMode.LIVE,
    ) -> DispatchResult:
        """Start one run per matching rule, across every ready activation.

        Each run is independent: one rule failing does not fail its siblings,
        which is the direct fix for V1 forcing every per-rule runner onto one
        run row and thereby onto one failure.
        """
        hydrated = await self._hydrate_event(event)
        dispatch_id = self._dispatch_id(hydrated)
        event_type = self._event_type(hydrated)

        refs: Sequence[ArtifactRef] = []
        pending: list[PendingEventRef] = []
        if self.activations is not None:
            refs = await self.activations.ready_activations(event_type)

        selected: list[str] = []
        run_ids: list[str] = []
        deduplicated: list[str] = []
        commands: list[tuple[str, str]] = []
        coroutines: list[Any] = []
        rule_order: list[tuple[ArtifactRef, str]] = []

        for ref in refs:
            try:
                artifact = self._load(ref)
            except Exception as exc:  # noqa: BLE001 - a load failure queues, never drops
                logger.warning("V2 dispatch: artifact %s unavailable: %s", ref.artifact_sha256, exc)
                pending.append(
                    PendingEventRef(ref.playbook_id, hydrated.get("event_id"), "unavailable")
                )
                await self._queue_pending(ref.playbook_id, hydrated)
                continue
            for rule in artifact.rules:
                if not self._trigger_matches(rule, event_type, hydrated):
                    continue
                selected.append(rule.id)
                rule_order.append((ref, rule.id))
                coroutines.append(
                    self.run_rule(
                        ref, rule.id, hydrated, principal, mode=mode, dispatch_id=dispatch_id
                    )
                )

        outcomes = await asyncio.gather(*coroutines, return_exceptions=True)
        for (ref, rule_id), outcome in zip(rule_order, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                # A sibling's crash is that sibling's failure, not the
                # dispatch's — the run row it left behind carries the error.
                logger.error("V2 rule %s of %s raised: %r", rule_id, ref.playbook_id, outcome)
                continue
            run_ids.append(outcome.run_id)
            if outcome.outcome == "deduplicated":
                deduplicated.append(rule_id)
            for receipt in outcome.receipts:
                step = self._step_of(ref, receipt.step_id)
                if isinstance(step, CommandStep):
                    commands.append((receipt.step_id, step.command))

        return DispatchResult(
            dispatch_id=dispatch_id,
            rules_selected=tuple(selected),
            run_ids=tuple(run_ids),
            pending=tuple(pending),
            deduplicated=tuple(deduplicated),
            commands=tuple(commands),
        )

    # ------------------------------------------------------------------
    # §3.5 — one rule, one run
    # ------------------------------------------------------------------

    async def run_rule(
        self,
        artifact_ref: ArtifactRef,
        rule_id: str,
        event: Mapping[str, Any],
        principal: Any,
        mode: ExecutionMode = ExecutionMode.LIVE,
        *,
        dispatch_id: str | None = None,
        deadline_at: float | None = None,
        pause_before_start: bool = False,
    ) -> RunOutcome:
        """Create one run for *rule_id* and walk it.

        ``pause_before_start`` creates the run row and stops, which is what a
        caller wants when the run must exist before it may execute: the
        dependency-unavailable path (§4.13) and an operator starting a run
        for later resumption both need a durable, addressable run at its
        entry step rather than a promise to make one.
        """
        artifact = self._load(artifact_ref)
        rule = next((r for r in artifact.rules if r.id == rule_id), None)
        if rule is None:
            raise KeyError(f"artifact {artifact.id} has no rule {rule_id!r}")

        repository = self._repository(mode)
        dispatch_id = dispatch_id or self._dispatch_id(event)
        now = self.services.clock()
        snapshot = RunSnapshot(
            run_id=uuid.uuid4().hex,
            playbook_id=artifact.id,
            artifact_sha256=artifact_ref.artifact_sha256,
            rule_id=rule.id,
            lifecycle=RunLifecycle.PAUSED if pause_before_start else RunLifecycle.RUNNING,
            mode=mode.value,
            current_step_id=rule.entry_step,
            event=dict(event),
            context=self._context(artifact, rule, dispatch_id),
            event_type=self._event_type(event),
            event_id=event.get("event_id"),
            dispatch_id=dispatch_id,
            deadline_at=deadline_at,
            started_at=now,
            updated_at=now,
        )
        try:
            snapshot = await repository.create_run(snapshot)
        except DuplicateRun:
            existing = await repository.find_run_for_dispatch(dispatch_id, rule.id)
            if existing is None:  # pragma: no cover - the index said it exists
                raise
            return RunOutcome(existing.run_id, existing.lifecycle, "deduplicated", existing)

        await self._emit(EVENT_RUN_STARTED, snapshot)
        if pause_before_start:
            return RunOutcome(snapshot.run_id, snapshot.lifecycle, "paused", snapshot)
        return await self._walk(snapshot, artifact, artifact_ref, principal, mode, repository)

    # ------------------------------------------------------------------
    # §3.5 — resume
    # ------------------------------------------------------------------

    async def resume(self, run_id: str, cause: ResumeCause, principal: Any) -> RunOutcome:
        """Continue a paused run **against the same artifact**.

        Never against a rebuilt one: a run reads its graph from its pinned
        artifact hash, and re-resolving it at resume time would be the
        in-place translation the roadmap forbids.
        """
        repository = self.runs
        if repository is None:
            raise RuntimeError("resume requires a run repository")
        snapshot = await repository.load_run(run_id)
        if snapshot is None:
            return RunOutcome(run_id, RunLifecycle.FAILED, "unknown_run")
        if snapshot.is_terminal:
            return RunOutcome(run_id, snapshot.lifecycle, "already_terminal", snapshot)

        mode = ExecutionMode(snapshot.mode)
        artifact_ref = await self._ref_for(snapshot)
        artifact = self._load(artifact_ref)
        if isinstance(cause, ChildTaskCompleted):
            return await self._resume_child_task(
                snapshot, cause, principal, artifact, artifact_ref, mode, repository
            )
        snapshot = replace(
            snapshot,
            lifecycle=RunLifecycle.RUNNING,
            context=dict(snapshot.context) | self._resume_context(cause),
            updated_at=self.services.clock(),
        )
        claim = self._claim_for_cause(snapshot, cause)
        if claim is not None:
            snapshot = replace(
                snapshot, pending_wait_claims=snapshot.pending_wait_claims + (claim,)
            )
        return await self._walk(snapshot, artifact, artifact_ref, principal, mode, repository)

    async def _resume_child_task(
        self,
        snapshot: RunSnapshot,
        cause: ChildTaskCompleted,
        principal: Any,
        artifact: PlaybookDefinition,
        artifact_ref: ArtifactRef,
        mode: ExecutionMode,
        repository: Any,
    ) -> RunOutcome:
        """§4.5 step 5 — reconcile one child completion, exactly once.

        The registered wait *is* the idempotency token.  The first delivery
        clears it in the same boundary that takes the edge, so a second
        delivery of the same ``(run_id, step_id, task_id)`` finds nothing to
        reconcile and returns without a receipt and without a transition.
        That is why this does not re-enter the executor: re-running an
        ``AgentTaskStep`` would create a *second* child task, which is the
        expensive form of a duplicate side effect.
        """
        wait = snapshot.wait
        step_id = snapshot.current_step_id
        step = artifact.steps.get(step_id) if step_id else None
        if (
            wait is None
            or wait.kind != "agent_task"
            or wait.match.get("task_id") != cause.task_id
            or not isinstance(step, AgentTaskStep)
        ):
            logger.info(
                "v2 run %s ignored a duplicate child completion for task %s",
                snapshot.run_id,
                cause.task_id,
            )
            return RunOutcome(
                snapshot.run_id, snapshot.lifecycle, "duplicate_child_completion", snapshot
            )

        now = self.services.clock()
        running = replace(
            snapshot,
            lifecycle=RunLifecycle.RUNNING,
            context=dict(snapshot.context) | self._resume_context(cause),
            updated_at=now,
        )
        iteration = self._iteration_of(running, step_id)
        attempt = _Attempt(
            snapshot=running,
            step_id=step_id or "",
            step=step,
            started_at=now,
            principal=principal,
            iteration=iteration,
            attempt=self._next_attempt(running, step_id or "", iteration),
        )
        attempt.idempotency_key = idempotency_key(
            running.run_id, step_id or "", iteration, attempt.attempt
        )
        attempt.outcome = child_outcome_for_status(cause.status)
        attempt.clear_waits = True
        attempt.wait_id = wait.wait_id
        attempt.wait_changes = WaitChangeSet(clear_run_waits=True)
        attempt.receipt_result = {"child_task_id": cause.task_id, "child_status": cause.status}
        if attempt.outcome == "runtime_error":
            attempt.error = f"child status {cause.status!r} has no mapped outcome"
        if attempt.outcome == "timed_out":
            attempt.timed_out = True
        if step.save_result_as:
            try:
                attempt.snapshot = bind_step_output(
                    running,
                    step_id=step.save_result_as,
                    value={"task_id": cause.task_id, "status": cause.status},
                    declared=("task_id", "status"),
                )
            except StateLimitExceeded:
                attempt.outcome = "state_limit_exceeded"
                attempt.error = "bound child result exceeds the state limit"

        committed, receipt, outcome = await self._advance_on_outcome(
            attempt, artifact, artifact_ref, repository
        )
        if receipt is not None:
            await self._emit(EVENT_STEP_COMPLETED, committed, step_id=receipt.step_id)
        if committed.is_terminal or committed.lifecycle is not RunLifecycle.RUNNING:
            await self._emit(EVENT_RUN_FINISHED, committed, outcome=outcome)
            return RunOutcome(
                committed.run_id,
                committed.lifecycle,
                outcome,
                committed,
                tuple(r for r in (receipt,) if r is not None),
            )
        walked = await self._walk(committed, artifact, artifact_ref, principal, mode, repository)
        return replace(
            walked,
            receipts=tuple(r for r in (receipt,) if r is not None) + walked.receipts,
        )

    async def cancel(
        self,
        run_id: str,
        principal: Any,
        *,
        reason: str = "operator",
        cancel_children: bool | None = None,
    ) -> RunOutcome:
        """Record a cancellation intent durably.

        The intent is durable *before* the engine has done anything about it,
        which is what stops a live run from overwriting it (the failure the
        V1 command documents in its own docstring).  Acknowledging an
        in-flight executor within the grace window is T-9's, not this
        commit's.
        """
        repository = self.runs
        if repository is None:
            raise RuntimeError("cancel requires a run repository")
        snapshot = await repository.load_run(run_id)
        if snapshot is None:
            return RunOutcome(run_id, RunLifecycle.FAILED, "unknown_run")
        if snapshot.is_terminal:
            return RunOutcome(run_id, snapshot.lifecycle, "already_terminal", snapshot)
        updated = await repository.request_cancel(
            run_id,
            expected_version=snapshot.version,
            reason=reason,
            requested_by=getattr(principal, "describe", lambda: "operator")(),
        )
        await self._cancel_children(updated, principal, cancel_children)
        return RunOutcome(run_id, updated.lifecycle, "cancel_requested", updated)

    async def _cancel_children(
        self, snapshot: RunSnapshot, principal: Any, cancel_children: bool | None
    ) -> None:
        """§4.9 and §7.4 — propagate cancellation without granting authority.

        ``cancel_children=None`` means "use each ``AgentTaskStep``'s
        ``cancel_child``", which the model defaults to ``False``: cancelling
        a parent leaves shared or reused child work running unless someone
        said otherwise.  The stop is dispatched as the **narrowed child**
        principal, re-derived from the parent's *current* policy, so a parent
        whose authority has since shrunk cannot reach the child through the
        cancel path.
        """
        if not snapshot.agent_task_ids:
            return
        step = None
        if snapshot.current_step_id:
            artifact_ref = await self._ref_for(snapshot)
            step = self._load(artifact_ref).steps.get(snapshot.current_step_id)
        if not isinstance(step, AgentTaskStep):
            return
        if not (step.cancel_child if cancel_children is None else cancel_children):
            return
        policy, reason = await resolve_profile_policy(self.services, step.profile_id)
        if policy is None:
            logger.warning(
                "v2 run %s could not cancel its child: %s", snapshot.run_id, reason
            )
            return
        child_principal = narrow_for_child(
            step, principal, policy, snapshot.current_step_id or ""
        )
        for task_id in snapshot.agent_task_ids:
            cancelled, diagnostic = await cancel_child_task(
                task_id, principal=child_principal, services=self.services
            )
            if not cancelled:
                logger.warning("v2 run %s: %s", snapshot.run_id, diagnostic)

    # ------------------------------------------------------------------
    # The walk
    # ------------------------------------------------------------------

    async def _walk(
        self,
        snapshot: RunSnapshot,
        artifact: PlaybookDefinition,
        artifact_ref: ArtifactRef,
        principal: Any,
        mode: ExecutionMode,
        repository: Any,
    ) -> RunOutcome:
        receipts: list[StepReceipt] = []
        visits = 0
        outcome = "completed"
        while not snapshot.is_terminal and snapshot.lifecycle is RunLifecycle.RUNNING:
            visits += 1
            if visits > self.max_step_visits:
                snapshot, receipt = await self._terminate(
                    snapshot, repository, "state_limit_exceeded", "step visit limit exceeded"
                )
                receipts.append(receipt)
                outcome = "state_limit_exceeded"
                break
            snapshot, receipt, outcome = await self._advance_one_step(
                snapshot, artifact, artifact_ref, principal, mode, repository
            )
            if receipt is not None:
                receipts.append(receipt)
                await self._emit(EVENT_STEP_COMPLETED, snapshot, step_id=receipt.step_id)
            if snapshot.lifecycle is RunLifecycle.PAUSED and snapshot.pending_wait_claims:
                # Registration and inbox ingestion serialize per playbook, so
                # an event that arrived first is consumed *inside* the same
                # boundary that opened the wait and handed back on the
                # snapshot.  Sleeping on it would lose the only delivery.
                snapshot = replace(snapshot, lifecycle=RunLifecycle.RUNNING)
        await self._emit(EVENT_RUN_FINISHED, snapshot, outcome=outcome)
        return RunOutcome(snapshot.run_id, snapshot.lifecycle, outcome, snapshot, tuple(receipts))

    async def _advance_one_step(
        self,
        snapshot: RunSnapshot,
        artifact: PlaybookDefinition,
        artifact_ref: ArtifactRef,
        principal: Any,
        mode: ExecutionMode,
        repository: Any,
    ) -> tuple[RunSnapshot, StepReceipt | None, str]:
        """§3.4's eleven stages, in order.

        Stages 8-10 are the atomic unit: nothing between the executor call
        and the commit writes anything durable, so a crash in that window
        loses the attempt and never the run.
        """
        step_id = snapshot.current_step_id
        if step_id is None or step_id not in artifact.steps:
            return await self._fail_now(
                snapshot, repository, "contract_violation", f"step {step_id!r} is not in the artifact"
            )
        step = artifact.steps[step_id]
        iteration = self._iteration_of(snapshot, step_id)
        attempt = _Attempt(
            snapshot=snapshot,
            step_id=step_id,
            step=step,
            started_at=self.services.clock(),
            principal=principal,
            iteration=iteration,
            attempt=self._next_attempt(snapshot, step_id, iteration),
        )
        attempt.idempotency_key = idempotency_key(
            snapshot.run_id, step_id, iteration, attempt.attempt
        )

        # 2. Cancellation — read from the snapshot the engine is about to
        #    write, so a live run cannot overwrite a cancellation.
        if snapshot.cancel_requested_at is not None:
            attempt.outcome = "cancelled"
            attempt.lifecycle = RunLifecycle.CANCELLED
            attempt.cancelled_at = attempt.started_at
            return await self._commit(attempt, artifact_ref, repository)

        # 3. Deadline.
        if snapshot.deadline_at is not None and attempt.started_at >= snapshot.deadline_at:
            attempt.outcome = "timed_out"
            attempt.lifecycle = RunLifecycle.TIMED_OUT
            attempt.timed_out = True
            attempt.error = "run deadline fired"
            return await self._commit(attempt, artifact_ref, repository)

        # A suspended run resumes *without* re-running its wait executor:
        # re-executing would compute a second correlation key and open a
        # second wait for one suspension.  The resumption is durable state
        # on the snapshot, so this branch is identical after a restart.
        if isinstance(step, WaitStep) and snapshot.wait is not None:
            return await self._resume_wait(attempt, step, artifact, artifact_ref, repository)

        scope = self._scope(snapshot, artifact)

        # 4. Resolve inputs.  A miss is an outcome *before* the executor
        #    runs; the engine never injects a marker and never coerces to "".
        try:
            inputs = {
                name: resolve_value(value, scope)
                for name, value in getattr(step, "inputs", {}).items()
            }
        except ValueResolutionError as exc:
            attempt.outcome = "input_resolution_failed"
            attempt.error = exc.reason
            return await self._advance_on_outcome(attempt, artifact, artifact_ref, repository)

        # 5. Authorize.  Package 0 owns the decision; the engine only routes
        #    it, so an artifact can declare an edge for a denial.
        if isinstance(step, CommandStep) and not self._authorized(step, principal):
            attempt.outcome = "unauthorized"
            attempt.error = f"capability denied: {step.command}"
            return await self._advance_on_outcome(attempt, artifact, artifact_ref, repository)

        ctx = StepContext(
            run_id=snapshot.run_id,
            dispatch_id=snapshot.dispatch_id or "",
            artifact_ref=artifact_ref,
            artifact=artifact,
            rule_id=snapshot.rule_id,
            step_id=step_id,
            principal=principal,
            scope=scope,
            services=self.services,
            mode=mode,
            attempt=attempt.attempt,
            iteration_index=None if iteration < 0 else iteration,
            run_deadline_at=snapshot.deadline_at,
            cancel_requested=snapshot.cancel_requested_at is not None,
            inputs=inputs,
            loop_frame=snapshot.loop,
        )

        # 6. Execute.  An unexpected exception is runtime_error carrying the
        #    exception *type*; a message can carry an argument value.
        executor = executor_for(step.type, mode)
        try:
            result = await executor.execute(step, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - §3.4 step 6
            result = ExecutorResult(
                control=StepControl.ADVANCE,
                outcome="runtime_error",
                diagnostics=(type(exc).__name__,),
            )

        attempt.receipt_inputs = result.receipt_inputs
        attempt.receipt_result = result.receipt_result
        attempt.idempotency_key = result.idempotency_key or attempt.idempotency_key
        if result.diagnostics:
            attempt.error = "; ".join(result.diagnostics)

        # 7. Validate the result.
        violation = self.validate_control(step, result)
        if violation is not None:
            attempt.outcome = violation
            attempt.error = f"{result.control} is not legal for a {step.type} step"
            return await self._advance_on_outcome(attempt, artifact, artifact_ref, repository)

        attempt.control = result.control
        attempt.outcome = result.outcome
        attempt.loop_frame = result.loop_frame
        attempt.clear_loop = result.clear_loop

        # 8. Bind, then the size checks.
        if result.value is not None and getattr(step, "save_result_as", None):
            try:
                attempt.snapshot = bind_step_output(
                    snapshot,
                    step_id=step.save_result_as,
                    value=result.value,
                    declared=result.value.keys()
                    if isinstance(result.value, Mapping)
                    else (),
                )
            except StateLimitExceeded:
                attempt.outcome = "state_limit_exceeded"
                attempt.error = "bound result exceeds the state limit"
                return await self._advance_on_outcome(attempt, artifact, artifact_ref, repository)

        # 9 and 10.
        if result.control is StepControl.TERMINATE:
            attempt.lifecycle = _TERMINAL_LIFECYCLE.get(
                result.terminal_outcome or "completed", RunLifecycle.COMPLETED
            )
            attempt.next_step_id = None
            return await self._commit(attempt, artifact_ref, repository)
        if result.control is StepControl.GOTO:
            attempt.next_step_id = result.goto_step_id
            attempt.selected_transition = transition_id(
                snapshot.rule_id, step_id, result.outcome
            )
            return await self._commit(attempt, artifact_ref, repository)
        if result.control is StepControl.SUSPEND:
            # One transaction: the paused snapshot, the receipt, and the
            # registration that scans the durable inbox for an event that
            # already arrived.  Nothing between deciding to wait and being
            # findable by an event is observable.
            # ``validate_control`` already proved a SUSPEND carries one.
            wait = result.wait
            attempt.lifecycle = RunLifecycle.PAUSED
            attempt.wait = wait
            attempt.wait_id = wait.wait_id
            attempt.wait_changes = WaitChangeSet(
                register=(wait,), clear_run_waits=result.child_task_id is not None
            )
            attempt.child_task_id = result.child_task_id
            attempt.next_step_id = attempt.step_id
            return await self._commit(attempt, artifact_ref, repository)
        if result.control is StepControl.UNRESOLVED:
            attempt.lifecycle = RunLifecycle.PAUSED
            attempt.error = attempt.error or "unresolved boundary"
            return await self._commit(attempt, artifact_ref, repository)
        if result.control is StepControl.OPERATOR_DECISION:
            attempt.lifecycle = RunLifecycle.PAUSED
            attempt.outcome = "operator_decision_required"
            return await self._commit(attempt, artifact_ref, repository)
        return await self._advance_on_outcome(attempt, artifact, artifact_ref, repository)

    def validate_control(self, step: Any, result: ExecutorResult) -> str | None:
        """§3.1.3 and §3.4 step 7 — control/field coherence.

        The ``GOTO`` half is the mechanical form of "runtime output cannot
        alter control flow unless the typed step contract explicitly exposes
        the referenced field": only a decision and a foreach declare a
        runtime-chosen target, so a ``GOTO`` from anything else is a
        contract violation by construction, not by review.
        """
        if result.control is StepControl.GOTO:
            if step.type not in GOTO_CAPABLE_STEP_KINDS:
                return "contract_violation"
            if result.goto_step_id not in set(step_targets(step).values()):
                return "contract_violation"
        if result.control is StepControl.SUSPEND and result.wait is None:
            return "contract_violation"
        if result.control is StepControl.TERMINATE and not result.terminal_outcome:
            return "contract_violation"
        return None

    async def _advance_on_outcome(
        self,
        attempt: _Attempt,
        artifact: PlaybookDefinition,
        artifact_ref: ArtifactRef,
        repository: Any,
    ) -> tuple[RunSnapshot, StepReceipt | None, str]:
        """§3.4 step 9 — select the transition, or fail loudly.

        A *business* outcome with no edge is a contract violation, never a
        silent completion.  Only a reserved outcome may fall back to the
        artifact's ``runtime_error`` catch-all.
        """
        step = attempt.step
        transitions: Mapping[str, str] = getattr(step, "transitions", {})
        outcome = attempt.outcome
        target = transitions.get(outcome)
        if target is None and outcome in ENGINE_RESERVED_OUTCOMES:
            target = transitions.get("runtime_error")
        if target is None:
            attempt.outcome = (
                outcome if outcome in ENGINE_RESERVED_OUTCOMES else "contract_violation"
            )
            attempt.lifecycle = RunLifecycle.FAILED
            attempt.error = attempt.error or (
                f"outcome {outcome!r} has no transition and no runtime_error edge"
            )
            if attempt.outcome == "timed_out":
                attempt.lifecycle = RunLifecycle.TIMED_OUT
            elif attempt.outcome == "cancelled":
                attempt.lifecycle = RunLifecycle.CANCELLED
            return await self._commit(attempt, artifact_ref, repository)
        attempt.next_step_id = target
        attempt.selected_transition = transition_id(
            attempt.snapshot.rule_id, attempt.step_id, outcome
        )
        return await self._commit(attempt, artifact_ref, repository)

    # ------------------------------------------------------------------
    # §3.4 steps 10 and 11 — the one durable write
    # ------------------------------------------------------------------

    async def _commit(
        self, attempt: _Attempt, artifact_ref: ArtifactRef, repository: Any
    ) -> tuple[RunSnapshot, StepReceipt | None, str]:
        now = self.services.clock()
        snapshot = attempt.snapshot
        terminal = attempt.lifecycle in {
            RunLifecycle.COMPLETED,
            RunLifecycle.FAILED,
            RunLifecycle.TIMED_OUT,
            RunLifecycle.CANCELLED,
        }
        attempts = dict(snapshot.attempts)
        attempts[f"{attempt.step_id}:{attempt.iteration}"] = attempt.attempt
        agent_task_ids = snapshot.agent_task_ids
        if attempt.child_task_id and attempt.child_task_id not in agent_task_ids:
            agent_task_ids = agent_task_ids + (attempt.child_task_id,)
        next_snapshot = replace(
            snapshot,
            lifecycle=attempt.lifecycle,
            agent_task_ids=agent_task_ids,
            current_step_id=attempt.next_step_id or attempt.step_id,
            wait=self._next_wait(attempt),
            loop=self._next_loop(attempt),
            attempts=attempts,
            error=attempt.error,
            error_code=(
                attempt.outcome if attempt.outcome in ENGINE_RESERVED_OUTCOMES else None
            ),
            updated_at=now,
            completed_at=now if terminal else None,
        )
        receipt = StepReceipt(
            receipt_id=uuid.uuid4().hex,
            run_id=snapshot.run_id,
            artifact_sha256=artifact_ref.artifact_sha256,
            rule_id=snapshot.rule_id,
            step_id=attempt.step_id,
            step_kind=attempt.step.type,
            outcome=self._classify(attempt),
            started_at=attempt.started_at,
            # The version this boundary *writes*, not the one it read:
            # ``commit_boundary`` advances the snapshot itself, and a receipt
            # naming the pre-boundary version is a ``RunIdentityMismatch``.
            snapshot_version=snapshot.version + 1,
            iteration=attempt.iteration,
            attempt=attempt.attempt,
            idempotency_key=attempt.idempotency_key,
            contract_fingerprint=self._contract_fingerprint(attempt.step),
            principal=self._principal_projection(attempt.principal),
            inputs=dict(attempt.receipt_inputs),
            result=dict(attempt.receipt_result),
            selected_transition=attempt.selected_transition,
            error=attempt.error,
            error_code=(
                attempt.outcome if attempt.outcome in ENGINE_RESERVED_OUTCOMES else None
            ),
            wait_id=attempt.wait_id,
            timed_out=attempt.timed_out,
            cancelled_at=attempt.cancelled_at,
            completed_at=now,
            duration_ms=max(0, int((now - attempt.started_at) * 1000)),
        )
        wait_changes = attempt.wait_changes
        if attempt.clear_waits and wait_changes.is_empty:
            wait_changes = WaitChangeSet(clear_run_waits=True)
        try:
            committed = await repository.commit_boundary(next_snapshot, receipt, wait_changes)
        except SnapshotVersionConflict:
            # Two writers at one boundary means two engines think they own
            # the run.  The write is never retried: silently merging them is
            # how a side-effecting command runs twice.
            failed, error_receipt = await self._terminate(
                snapshot, repository, "interrupted", "another writer advanced this run"
            )
            return failed, error_receipt, "interrupted"
        return committed, receipt, attempt.outcome

    async def _terminate(
        self, snapshot: RunSnapshot, repository: Any, outcome: str, error: str
    ) -> tuple[RunSnapshot, StepReceipt]:
        """Fail a run with an error receipt, without a second boundary write.

        Reached only when the *first* boundary already failed, so the receipt
        is built here and stored on a best-effort basis: the run row is what
        an operator sees, and it must say why even if the receipt insert is
        what broke.
        """
        now = self.services.clock()
        failed = replace(
            snapshot,
            lifecycle=RunLifecycle.FAILED,
            error=error,
            error_code=outcome,
            updated_at=now,
            completed_at=now,
        )
        receipt = StepReceipt(
            receipt_id=uuid.uuid4().hex,
            run_id=snapshot.run_id,
            artifact_sha256=snapshot.artifact_sha256,
            rule_id=snapshot.rule_id,
            step_id=snapshot.current_step_id or "",
            step_kind="command",
            outcome="failure",
            started_at=now,
            snapshot_version=snapshot.version + 1,
            error=error,
            error_code=outcome,
            completed_at=now,
        )
        try:
            await repository.commit_boundary(failed, receipt)
        except Exception:
            logger.exception("V2 run %s could not receipt its failure", snapshot.run_id)
        return failed, receipt

    async def _fail_now(
        self, snapshot: RunSnapshot, repository: Any, outcome: str, error: str
    ) -> tuple[RunSnapshot, StepReceipt | None, str]:
        failed, receipt = await self._terminate(snapshot, repository, outcome, error)
        return failed, receipt, outcome

    # ------------------------------------------------------------------
    # §4.6 — the suspension's other half
    # ------------------------------------------------------------------

    async def _resume_wait(
        self,
        attempt: _Attempt,
        step: WaitStep,
        artifact: PlaybookDefinition,
        artifact_ref: ArtifactRef,
        repository: Any,
    ) -> tuple[RunSnapshot, StepReceipt | None, str]:
        """Turn a claimed wait into an outcome, in one boundary.

        The wait is deregistered in the *same* change set that advances the
        run, so there is no window in which a run has moved on while its wait
        is still claimable — which is what would let one suspension resume
        twice.
        """
        snapshot = attempt.snapshot
        wait = snapshot.wait
        if wait is None:  # pragma: no cover - the caller checked
            return replace(snapshot, lifecycle=RunLifecycle.PAUSED), None, "paused"
        claim = next(
            (c for c in snapshot.pending_wait_claims if c.wait_id == wait.wait_id), None
        )
        if claim is None:
            # Resumed with nothing to resume on.  No boundary, no receipt:
            # the durable row still says paused and must keep saying so.
            return replace(snapshot, lifecycle=RunLifecycle.PAUSED), None, "paused"

        remaining = tuple(c for c in snapshot.pending_wait_claims if c.wait_id != wait.wait_id)
        attempt.snapshot = replace(snapshot, pending_wait_claims=remaining)
        attempt.wait_id = wait.wait_id
        attempt.wait_changes = WaitChangeSet(clear_wait_ids=(wait.wait_id,))

        outcome, value = resolve_wait_result(
            step,
            WaitResumption(
                expired=claim.expired,
                event_type=claim.event_type,
                payload=dict(claim.event_fields),
                at=claim.claimed_at,
            ),
        )
        attempt.outcome = outcome
        if outcome == "timed_out":
            attempt.timed_out = True
            attempt.error = f"{self._deadline_that_fired(snapshot, step, wait)} deadline fired"
        elif outcome == "contract_violation":
            attempt.error = "the resolution is not one of the gate's declared outcomes"

        if value is not None and step.save_result_as:
            try:
                attempt.snapshot = bind_step_output(
                    attempt.snapshot,
                    step_id=step.save_result_as,
                    value=value,
                    declared=value.keys(),
                )
            except StateLimitExceeded:
                attempt.outcome = "state_limit_exceeded"
                attempt.error = "the wait result exceeds the state limit"
        return await self._advance_on_outcome(attempt, artifact, artifact_ref, repository)

    def _deadline_that_fired(
        self, snapshot: RunSnapshot, step: WaitStep, wait: WaitSpec
    ) -> str:
        """Which deadline expired — the wait's own, or the whole run's.

        ``WaitSpec.deadline_at`` is already the earlier of the two, so the
        answer is recovered by asking what the wait's own timeout would have
        been.  There is no ``deadline_fired`` receipt column (§2.5 item 2), so
        it lands in ``error`` beside ``timed_out``.
        """
        if snapshot.deadline_at is None:
            return "wait"
        if step.timeout_seconds is None:
            return "run"
        own = wait.created_at + step.timeout_seconds
        return "run" if snapshot.deadline_at <= own else "wait"

    @staticmethod
    def _claim_for_cause(snapshot: RunSnapshot, cause: ResumeCause) -> WaitClaim | None:
        """Express a resume cause as the claim the wait path already consumes.

        One durable channel for all of them: an inbox match consumed inside
        the registration transaction arrives as a ``WaitClaim`` on the
        snapshot, and an externally delivered resume becomes the same shape
        here.  Two channels would mean two resume paths and one of them
        would eventually stop being restart-safe.
        """
        wait = snapshot.wait
        if wait is None:
            return None
        payload: dict[str, Any] = {}
        expired = False
        event_type = ""
        event_id: str | None = None
        if isinstance(cause, EventArrived):
            payload = dict(cause.payload)
            event_type = str(payload.get("event_type") or wait.event_type)
            event_id = cause.event_id
        elif isinstance(cause, TimerFired):
            if cause.wait_id and cause.wait_id != wait.wait_id:
                return None
            expired = True
        elif isinstance(cause, HumanDecision):
            payload = {"resolution": cause.decision, **dict(cause.payload)}
        elif isinstance(cause, ChildTaskCompleted):
            payload = {"task_id": cause.task_id, "status": cause.status}
        else:
            return None
        return WaitClaim(
            wait_id=wait.wait_id,
            run_id=snapshot.run_id,
            step_id=wait.step_id,
            iteration=wait.iteration,
            kind=wait.kind,
            snapshot_version=snapshot.version,
            claimed_event_id=event_id,
            claimed_at=snapshot.updated_at,
            expired=expired,
            event_type=event_type,
            event_fields=payload,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iteration_of(snapshot: RunSnapshot, step_id: str) -> int:
        """The loop iteration a step attempt belongs to.

        The loop *node* itself is not inside its own body, but it is reached
        once per iteration, so its receipts carry the index they closed —
        which is also what keeps their four-part attempt keys distinct.
        """
        return -1 if snapshot.loop is None else snapshot.loop.index

    @staticmethod
    def _next_attempt(snapshot: RunSnapshot, step_id: str, iteration: int) -> int:
        return int(snapshot.attempts.get(f"{step_id}:{iteration}", 0)) + 1

    @staticmethod
    def _next_wait(attempt: _Attempt) -> WaitSpec | None:
        if attempt.wait is not None:
            return attempt.wait
        if attempt.clear_waits:
            return None
        changes = attempt.wait_changes
        if changes.clear_wait_ids or changes.clear_run_waits:
            return None
        return attempt.snapshot.wait

    def _next_loop(self, attempt: _Attempt) -> LoopFrame | None:
        """The loop frame this boundary persists (§4.7).

        Three cases, in order: the loop ended, the loop executor computed the
        next frame, or a body step transitioned *back into* the loop node —
        which is how an author says "this outcome is per-item", and is where
        the iteration's verdict is recorded so the loop executor never has to
        re-derive it.
        """
        if attempt.clear_loop:
            return None
        if attempt.loop_frame is not None:
            return attempt.loop_frame
        frame = attempt.snapshot.loop
        if frame is None or attempt.next_step_id != frame.step_id:
            return frame
        if attempt.step_id == frame.step_id:
            return frame
        return replace(
            frame,
            last_step_id=attempt.step_id,
            last_outcome=attempt.outcome,
            last_failed=self._is_iteration_failure(attempt),
        )

    def _is_iteration_failure(self, attempt: _Attempt) -> bool:
        """§4.7's locked classification of the edge that re-entered the loop."""
        if attempt.outcome in ENGINE_RESERVED_OUTCOMES:
            return True
        return (
            self._declared_classification(attempt.step, attempt.outcome)
            is OutcomeClass.FAILURE
        )


    def _repository(self, mode: ExecutionMode) -> Any:
        """§3.3.5 — the real repository is passed only for LIVE."""
        if mode is ExecutionMode.LIVE:
            if self.runs is None:
                raise RuntimeError("a live run requires a run repository")
            return self.runs
        return InMemoryRunRecorder()

    def _load(self, ref: ArtifactRef) -> PlaybookDefinition:
        store = self.services.artifact_store
        if store is None:
            raise RuntimeError("the engine requires an artifact store")
        return store.load(ref.artifact_sha256)

    def _step_of(self, ref: ArtifactRef, step_id: str) -> Any:
        try:
            return self._load(ref).steps.get(step_id)
        except Exception:  # noqa: BLE001 - a projection helper never fails a dispatch
            return None

    @staticmethod
    def _event_type(event: Mapping[str, Any]) -> str:
        return str(
            event.get("_event_type") or event.get("event_type") or event.get("type") or ""
        )

    @staticmethod
    def _dispatch_id(event: Mapping[str, Any]) -> str:
        """Deterministic in the event id — §2.5 item 9.

        Sibling rule runs share it, and a replay of the same event collides
        on ``uq_playbook_v2_runs_dispatch_rule`` rather than on a pre-read
        that a concurrent dispatch could race.
        """
        event_id = event.get("event_id")
        if not event_id:
            return uuid.uuid4().hex[:12]
        return hashlib.sha256(f"v2-dispatch|{event_id}".encode()).hexdigest()[:12]

    def _trigger_matches(
        self, rule: Rule, event_type: str, event: Mapping[str, Any]
    ) -> bool:
        """Subscription-level match: type, then the literal filter.

        The filter is deliberately not an expression tree — a subscription
        must be matchable without a run context — so this is equality and
        membership only.
        """
        if rule.trigger.event_type != event_type:
            return False
        for name, expected in (rule.trigger.filter or {}).items():
            actual = event.get(name)
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    async def _hydrate_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Attach ``event.task`` so a rule can reference any task attribute.

        Lifted verbatim from ``core.py:888-903`` including its ``asdict``
        fallback: Package 6's parity harness compares V1 and V2 over the same
        events, and a hydration difference would read as a rule-selection
        difference.
        """
        hydrated = dict(event)
        db = self.services.db
        if db is not None and hydrated.get("task_id") and "task" not in hydrated:
            try:
                task_row = await db.get_task(str(hydrated["task_id"]))
                if task_row is not None:
                    from dataclasses import asdict

                    try:
                        hydrated["task"] = asdict(task_row)
                    except Exception:  # noqa: BLE001 - not every row is a dataclass
                        hydrated["task"] = (
                            vars(task_row) if hasattr(task_row, "__dict__") else {}
                        )
            except Exception:
                logger.debug(
                    "V2 dispatch: could not hydrate event.task for task_id=%s",
                    hydrated.get("task_id"),
                    exc_info=True,
                )
        return hydrated

    def _context(
        self, artifact: PlaybookDefinition, rule: Rule, dispatch_id: str
    ) -> dict[str, Any]:
        """``ENGINE_CONTEXT_SCHEMA``'s paths, and nothing else.

        The schema is closed, so a ``ContextRef`` the compiler accepted always
        resolves and one it rejected can never appear here.
        """
        return {
            "dispatch_id": dispatch_id,
            "playbook_id": artifact.id,
            "rule_id": rule.id,
            "artifact_sha256": artifact.artifact_sha256(),
            "now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.services.clock())),
        }

    @staticmethod
    def _resume_context(cause: ResumeCause) -> dict[str, Any]:
        if isinstance(cause, HumanDecision):
            return {"resume_decision": cause.decision}
        if isinstance(cause, ChildTaskCompleted):
            return {"resume_child_status": cause.status}
        if isinstance(cause, OperatorResolution):
            return {"resume_resolution": cause.kind}
        return {}

    def _scope(
        self, snapshot: RunSnapshot, artifact: PlaybookDefinition, attempt: int = 1
    ) -> ResolutionScope:
        """The four namespaces, with the live loop item in its own one.

        The item is re-resolved from the pinned collection rather than stored
        on the frame: the frame carries the collection's *digest*, so a
        collection that changed under an active loop is a contract violation
        the executor reports, not a stale copy the scope quietly serves.
        """
        scope = ResolutionScope(
            event=dict(snapshot.event),
            context=dict(snapshot.context) | {"run_id": snapshot.run_id, "attempt": attempt},
            bindings=dict(snapshot.bindings),
            loop={},
        )
        frame = snapshot.loop
        if frame is None:
            return scope
        loop_step = artifact.steps.get(frame.step_id)
        if not isinstance(loop_step, ForEachStep):
            return scope
        try:
            collection = resolve_value(loop_step.collection, scope)
        except ValueResolutionError:
            # The loop executor reports it as ``input_resolution_failed`` on
            # its own next boundary; the scope's job is not to raise here.
            return scope
        if not isinstance(collection, list) or not 0 <= frame.index < len(collection):
            return scope
        return scope.with_loop_item(frame.item_binding, collection[frame.index], frame.index)

    def _authorized(self, step: CommandStep, principal: Any) -> bool:
        resolver = self.services.resolver
        if resolver is None:
            # No resolver wired: authorization is the *dispatch* boundary's
            # job and it is already enforced there.  The engine never
            # substitutes a check of its own — §7.1.
            return True
        decision = authorize_command(
            step.command,
            principal,
            resolver=resolver,
            mode=self.services.authorization_mode,
        )
        return decision.allowed

    def _contract_fingerprint(self, step: Any) -> str:
        if not isinstance(step, CommandStep):
            return ""
        try:
            return self.services.contracts.fingerprint(step.command)
        except Exception:  # noqa: BLE001 - an uncontracted command already failed
            return ""

    @staticmethod
    def _principal_projection(principal: Any) -> dict[str, Any]:
        """The identity a receipt records — §3.3.3, mapped by §2.5 item 2.

        The *fingerprint* of the capability policy, never the policy itself:
        an operator needs to know that the grant changed between two runs,
        and printing the grant would put a capability list in a surface that
        Package 5 renders to anyone who can read the overlay.
        """
        kind = getattr(principal, "kind", None)
        policy = getattr(principal, "policy", None)
        fingerprint = ""
        if policy is not None:
            try:
                fingerprint = policy.fingerprint()
            except Exception:  # noqa: BLE001 - a receipt never fails a run
                fingerprint = ""
        return {
            "kind": getattr(kind, "value", kind),
            "profile_id": getattr(principal, "profile_id", None),
            "session_id": getattr(principal, "session_id", None),
            "capability_fingerprint": fingerprint,
        }

    def _classify(self, attempt: _Attempt) -> str:
        """The receipt's six-value classification (§2.5 item 2).

        A *different* vocabulary from the step outcome, which lands in
        ``error_code`` and in ``selected_transition``.  Two failure-classified
        outcomes therefore stay distinguishable on the receipt even though
        both classify as ``failure`` — routing is by name, never by class,
        which is exactly the property ``pipeline_runner.py:145`` lost.
        """
        if attempt.outcome == "operator_decision_required":
            return "operator_decision_required"
        if attempt.timed_out or attempt.outcome == "timed_out":
            return "timeout"
        if attempt.outcome == "cancelled":
            return "cancelled"
        if attempt.control in (StepControl.UNRESOLVED, StepControl.SUSPEND):
            # A suspension decided nothing: no edge was selected and no
            # binding was written, and the resume boundary writes the receipt
            # that carries the wait's real outcome.  ``RECEIPT_OUTCOMES`` has
            # no "paused" member (§2.5 item 2), and calling a pause a success
            # would make every open wait look like a finished step.
            return "skipped"
        if attempt.outcome in ENGINE_RESERVED_OUTCOMES:
            return "failure"
        if attempt.lifecycle is RunLifecycle.FAILED:
            return "failure"
        if self._declared_classification(attempt.step, attempt.outcome) is OutcomeClass.FAILURE:
            return "failure"
        return "success"

    def _declared_classification(self, step: Any, outcome: str) -> OutcomeClass | None:
        """What the *contract* says a business outcome means.

        Read for the receipt and for metrics only — never for choosing an
        edge.  A command with two failure-classified outcomes must still be
        able to route them to different steps.
        """
        if not isinstance(step, CommandStep):
            return None
        registration = self.services.contracts.get(step.command)
        if registration is None:
            return None
        for spec in registration.contract.execution.outcomes:
            if spec.name == outcome:
                return spec.classification
        return None

    async def _ref_for(self, snapshot: RunSnapshot) -> ArtifactRef:
        if self.activations is None:
            raise RuntimeError("resume requires an activation repository")
        for ref in await self.activations.ready_activations(snapshot.event_type):
            if ref.artifact_sha256 == snapshot.artifact_sha256:
                return ref
        raise KeyError(f"no activation pins artifact {snapshot.artifact_sha256}")

    async def _queue_pending(self, playbook_id: str, event: Mapping[str, Any]) -> None:
        queue = getattr(self.activations, "queue_pending_event", None)
        if queue is None:
            return
        await queue(playbook_id, event)

    async def _emit(self, event_type: str, snapshot: RunSnapshot, **extra: Any) -> None:
        """Emitted **after** a successful commit only.

        An event before the commit would let a subscriber observe a step that
        a crash then un-happens.
        """
        bus = self.services.bus
        if bus is None:
            return
        await bus.emit(
            event_type,
            {
                "run_id": snapshot.run_id,
                "playbook_id": snapshot.playbook_id,
                "rule_id": snapshot.rule_id,
                "artifact_sha256": snapshot.artifact_sha256,
                "lifecycle": snapshot.lifecycle.value,
                "dispatch_id": snapshot.dispatch_id,
                **extra,
            },
        )


class WaitScheduler:
    """Owns per-run wait deadlines — Package 4 child plan §4.6.

    Deliberately **not** ``TimerService``.  ``src/timer_service.py`` schedules
    playbook *triggers*: its entries are cron-like, operator-visible and
    survive as configuration.  A per-run wait is none of those — it belongs to
    one suspended run and disappears with it — so synthesising a trigger for
    each one would put run state into the operator's trigger surface and make
    a cancelled run's timer somebody's to clean up.

    ``expire_due`` claims each due wait with the same compare-and-set an
    event claim uses, so a wait that an event claims in the same instant is
    expired by nobody and resumes exactly once.
    """

    def __init__(self, engine: PlaybookEngine, waits: Any, principal: Any) -> None:
        self._engine = engine
        self._waits = waits
        self._principal = principal

    async def tick(self, now: float | None = None, *, limit: int = 100) -> tuple[str, ...]:
        """Resume every run whose wait deadline has passed.  Returns the ids."""
        if self._waits is None:
            return ()
        moment = self._engine.services.clock() if now is None else now
        resumed: list[str] = []
        for claim in await self._waits.expire_due(moment, limit=limit):
            try:
                await self._engine.resume(
                    claim.run_id, TimerFired(claim.wait_id), self._principal
                )
            except Exception:
                # One stuck run never stalls the sweep.
                logger.exception(
                    "V2 wait %s could not resume run %s", claim.wait_id, claim.run_id
                )
                continue
            resumed.append(claim.run_id)
        return tuple(resumed)


__all__ = [
    "ChildTaskCompleted",
    "DispatchResult",
    "EventArrived",
    "HumanDecision",
    "InMemoryRunRecorder",
    "OperatorResolution",
    "PendingEventRef",
    "PlaybookEngine",
    "ResumeCause",
    "RunOutcome",
    "TimerFired",
    "WaitScheduler",
]
