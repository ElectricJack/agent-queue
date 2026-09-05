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
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from src.commands.authorization import authorize_command
from src.commands.principal import PrincipalKind
from src.commands.contracts.models import OutcomeClass
from src.llm.client import LLMToolTurn, LLMToolTurnBoundaryError
from src.llm.types import TokenUsage
from src.playbooks.artifact_ref import ArtifactRef
from src.playbooks.definition import (
    AgentTaskStep,
    CommandStep,
    ForEachStep,
    LlmStep,
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
from src.playbooks.executors.llm import resolve_profile_principal
from src.playbooks.executors.base import (
    ENGINE_RESERVED_OUTCOMES,
    GOTO_CAPABLE_STEP_KINDS,
    Cancellable,
    EngineServices,
    ExecutionMode,
    ExecutorResult,
    StepContext,
    StepControl,
    UnknownStepType,
)
from src.playbooks.executors.wait import WaitResumption, resolve_wait_result
from src.playbooks.expressions import (
    ResolutionScope,
    ValueResolutionError,
    evaluate_condition,
    resolve_value,
)
from src.playbooks.receipts import (
    ATTEMPT_SCOPED_RECEIPT_KINDS,
    StepReceipt,
    idempotency_key,
    transition_id,
)
from src.playbooks.run_state import (
    DuplicateAttempt,
    DuplicateRun,
    IllegalLifecycleTransition,
    LoopFrame,
    OperatorDecision,
    RunBudget,
    RunLifecycle,
    RunSnapshot,
    SnapshotVersionConflict,
    StateLimitExceeded,
    TERMINAL_LIFECYCLES,
    bind_step_output,
    canonical_json,
)
from src.playbooks.waits import EMPTY_WAIT_CHANGES, WaitChangeSet, WaitClaim, WaitSpec
from src.review_keys import flag_review_task_event

logger = logging.getLogger(__name__)

#: §4.10's ``max_paths`` — the hard bound on returned symbolic paths for one
#: dry-run or shadow traversal.
DEFAULT_MAX_SYMBOLIC_PATHS = 32
#: The roadmap's default bound for symbolic dry-run traversal.  Live walks use
#: an artifact-derived ceiling instead: a dry-run option must never change the
#: semantics of a real run.
DEFAULT_DRY_RUN_MAX_STEP_VISITS = 1000

#: Preserve the original protection for live graphs without loops.  ForEach
#: artifacts raise this floor according to their authored iteration bounds.
MIN_LIVE_STEP_VISITS = 1000

#: How long ``cancel`` waits for an in-flight executor to give the run back
#: before it ends the run itself (§4.9).  Mirrors
#: ``playbooks.cancellation_grace_seconds``, whose default this is; the engine
#: takes the number rather than the config object, because an executor sees
#: only :class:`EngineServices` and neither does the walk.
DEFAULT_CANCELLATION_GRACE_SECONDS = 30.0

#: ``receipt.result`` key carrying §4.9's ``acknowledged`` / ``grace_expired``
#: discriminator.  It rides the receipt's own JSON projection rather than a
#: new column: Package 4 owns no tables (§10), and the pair
#: ``(receipt.cancelled_at, receipt.result[CANCELLATION_KEY])`` already says
#: when the run stopped and whether the executor gave it back.
CANCELLATION_KEY = "cancellation"

#: The two values under :data:`CANCELLATION_KEY`.
CANCELLATION_ACKNOWLEDGED = "acknowledged"
CANCELLATION_GRACE_EXPIRED = "grace_expired"

#: Bus events.  Package 7 reads the two run-level ones by name.
EVENT_RUN_STARTED = "playbook.v2.run.started"
EVENT_STEP_COMPLETED = "playbook.v2.step.completed"

#: The step types whose attempts reach outside the engine, and are therefore
#: fenced by a receipted ``attempt_start`` boundary before they execute.
EXTERNAL_STEP_TYPES = (CommandStep, LlmStep, AgentTaskStep)
EVENT_RUN_FINISHED = "playbook.v2.run.finished"

_TERMINAL_LIFECYCLE: dict[str, RunLifecycle] = {
    "completed": RunLifecycle.COMPLETED,
    "failed": RunLifecycle.FAILED,
    "blocked": RunLifecycle.BLOCKED,
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
    #: Ordered ``(step_id, command_name, canonical_args)`` records for
    #: shadow parity.  They are in-memory observations, never invocations.
    commands: tuple[tuple[str, str, str], ...] = ()
    #: Shadow only: one bounded symbolic tree per selected rule, in rule
    #: order, carrying every decision the walk could compare — the edges a
    #: live run would take and the outcomes it forked across.
    traversals: tuple[DryRunTree, ...] = ()


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: str
    lifecycle: RunLifecycle
    outcome: str
    snapshot: RunSnapshot | None = None
    receipts: tuple[StepReceipt, ...] = ()
    commands: tuple[tuple[str, str, str], ...] = ()
    result_value: Any | None = None
    #: Operator-facing refusal text, when there is one.
    error: str | None = None
    #: Whether cancellation was acknowledged or its grace period expired.
    cancellation: str | None = None
    #: Shadow only: the bounded symbolic traversal that stands in for the
    #: cursor walk (§4.10).  ``None`` for a live run.
    traversal: DryRunTree | None = None


@dataclass(frozen=True, slots=True)
class DryRunNode:
    """One real-graph dry-run visit, including an honest unresolved boundary."""

    step_id: str
    status: str
    outcome: str | None = None
    target: str | None = None
    reason: str | None = None
    possible_outcomes: tuple[str, ...] = ()
    #: Present for a selected rule omitted before traversal due to the global
    #: path bound; regular nodes inherit their path's rule id.
    rule_id: str | None = None


@dataclass(frozen=True, slots=True)
class DryRunPath:
    """One bounded path through a selected rule's executable graph."""

    rule_id: str
    nodes: tuple[DryRunNode, ...]
    status: str
    completed: bool = False
    #: Selected rules omitted by the global path bound.  A frontier belongs
    #: inside a returned path so ``max_paths`` remains a hard result bound.
    omitted_frontiers: tuple[DryRunNode, ...] = ()


@dataclass(frozen=True, slots=True)
class DryRunTree:
    """The bounded real-graph answer returned by :meth:`PlaybookEngine.dry_run`."""

    artifact_sha256: str
    rules_selected: tuple[str, ...]
    paths: tuple[DryRunPath, ...]
    truncated: bool = False
    step_visits: int = 0


@dataclass(frozen=True, slots=True)
class _DryRunCursor:
    rule_id: str
    step_id: str
    scope: ResolutionScope
    loop: LoopFrame | None
    nodes: tuple[DryRunNode, ...] = ()
    unresolved: bool = False


class InMemoryRunRecorder:
    """The repository stand-in for dry-run and shadow (§3.3.5).

    The real repository is not merely unused in those modes — it is not
    wired in at all, which is the structural half of the no-side-effect
    proof.  Its behavioural half is T-12.
    """

    def __init__(self) -> None:
        self.snapshots: dict[str, RunSnapshot] = {}
        self.receipts: list[StepReceipt] = []
        self.commands: list[tuple[str, str, str]] = []

    def record_command(self, step_id: str, command: str, args: Mapping[str, Any]) -> None:
        """Remember a shadow command without making an external call.

        Deduplicated by its canonical triple: the symbolic walk forks, and a
        step reached again on another branch — or again through an edge
        that routes back to it — with identical arguments is one intended
        call, not one per fork.  Parity compares intended calls.
        """
        record = (step_id, command, canonical_json(args).decode("utf-8"))
        if record not in self.commands:
            self.commands.append(record)

    async def create_run(self, snapshot: RunSnapshot) -> RunSnapshot:
        self.snapshots[snapshot.run_id] = snapshot
        return snapshot

    async def load_run(self, run_id: str) -> RunSnapshot | None:
        return self.snapshots.get(run_id)

    async def find_run_for_dispatch(
        self, playbook_id: str, dispatch_id: str, rule_id: str
    ) -> RunSnapshot | None:
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
    #: §4.9 — set only on the boundary that ends a cancelled run.
    cancellation: str | None = None
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
    usage: TokenUsage | None = None
    llm_calls: int = 0
    boundary_receipts: list[StepReceipt] = field(default_factory=list)


@dataclass(slots=True)
class _RunControl:
    """The in-process handle :meth:`PlaybookEngine.cancel` reaches a walk by.

    Cancellation is the one operation that has to reach *inside* a step.  The
    durable intent alone is what stops a live run overwriting a cancellation
    (§3.4 step 2), but it does not stop the executor that is already running:
    without a handle, a thirty-minute LLM step keeps its side effects going
    for thirty minutes after the operator said stop.

    Nothing here is durable, and nothing here decides anything.  A restart
    drops the whole registry and the run is still ``cancelling`` in the
    database, so the next boundary — in whichever process picks the run up —
    reads the intent off the snapshot and ends it.  The handle only buys
    promptness within one process.
    """

    run_id: str
    #: Serialises cancellation intent/finalization with a completed LLM turn
    #: entering durable storage.  Once a turn callback owns this lock, grace
    #: expiry cannot overtake and discard that completed turn.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Set once that boundary is durable, by whichever side wrote it.
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    #: What ``request_cancel`` wrote.  The walk adopts its version, because
    #: recording the intent advanced the run row underneath the walk and a
    #: boundary written against the pre-cancel version is a lost cancellation
    #: reported as ``interrupted``.
    cancel_snapshot: RunSnapshot | None = None
    #: ``(executor, step, ctx)`` while a step is inside ``execute``.
    in_flight: tuple[Any, Any, StepContext] | None = None
    #: ``request_cancel`` has been delivered to the executor.  §4.9 allows the
    #: engine at most one signal per in-flight step, so a second ``cancel``
    #: call joins the first one's grace window instead of signalling again.
    signalled: bool = False
    #: Set before ``cancel`` waits for ``lock``.  A completed LLM callback
    #: that already owns the lock uses this request to persist cancellation
    #: itself rather than returning control for another provider call.
    pending_cancel: tuple[Any, str] | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancellation: str | None = None
    final: RunSnapshot | None = None
    receipt: StepReceipt | None = None


def _authored_idempotency_key(step: Any, scope: ResolutionScope) -> str | None:
    """Resolve a ``CommandStep``'s step-level idempotency override, if any.

    Resolved here rather than in the executor for the same reason
    ``step.inputs`` is: a resolution failure has to be an *outcome* before any
    executor runs, so the engine is the only thing that touches the scope.
    A ``ValueResolutionError`` therefore propagates to the same handler and
    becomes ``input_resolution_failed``.
    """
    raw = getattr(step, "idempotency_key", None)
    if raw is None:
        return None
    value = resolve_value(raw, scope)
    return None if value is None else str(value)


class PlaybookEngine:
    """Dispatch, single-rule execution and resume over the strict artifact."""

    def __init__(
        self,
        *,
        services: EngineServices,
        runs: Any | None = None,
        waits: Any | None = None,
        activations: Any | None = None,
        max_symbolic_paths: int = DEFAULT_MAX_SYMBOLIC_PATHS,
        max_symbolic_step_visits: int = DEFAULT_DRY_RUN_MAX_STEP_VISITS,
        cancellation_grace_seconds: float = DEFAULT_CANCELLATION_GRACE_SECONDS,
    ) -> None:
        self.services = services
        self.runs = runs
        self.waits = waits
        self.activations = activations
        #: The path bound a shadow run's symbolic traversal is held to.
        self.max_symbolic_paths = max_symbolic_paths
        #: The visit bound for shadow's symbolic traversal. Live execution
        #: derives its own ceiling from the artifact's authored loop bounds.
        self.max_symbolic_step_visits = max_symbolic_step_visits
        self.cancellation_grace_seconds = cancellation_grace_seconds
        #: Live walks in *this* process, keyed by run id.  See :class:`_RunControl`.
        self._live: dict[str, _RunControl] = {}

    # ------------------------------------------------------------------
    # §4.2 — dispatch
    # ------------------------------------------------------------------

    async def dispatch_event(
        self,
        event: Mapping[str, Any],
        principal: Any,
        mode: ExecutionMode = ExecutionMode.LIVE,
        *,
        playbook_ids: Collection[str] | None = None,
        dispatch_id: str | None = None,
        artifact_sha256: str | None = None,
    ) -> DispatchResult:
        """Start one run per matching rule, across every ready activation.

        Each run is independent: one rule failing does not fail its siblings,
        which is the direct fix for V1 forcing every per-rule runner onto one
        run row and thereby onto one failure.

        ``playbook_ids`` narrows the ready activations to the playbooks the
        caller has already admitted.  ``PlaybookManager`` applies role
        shadowing, cooldown and the concurrency cap *per playbook* and then
        invokes the trigger callback for that one playbook; without the
        filter the callback would start every scope-matching activation and
        silently overturn the sibling decisions the manager just made.
        ``None`` keeps the unconstrained event-level dispatch for callers
        that own admission themselves. ``dispatch_id`` lets a durable caller
        supply its own stable idempotency identity across process restarts;
        ordinary event dispatch derives the identity from the event as before.
        ``artifact_sha256`` bypasses mutable activation lookup and dispatches
        exactly one immutable artifact; it is reserved for callers that have
        already durably admitted and pinned that destination.
        """
        hydrated = await self._hydrate_event(event)
        dispatch_id = dispatch_id or self._dispatch_id(hydrated)
        event_type = self._event_type(hydrated)

        refs: Sequence[ArtifactRef] = []
        pending: list[PendingEventRef] = []
        if artifact_sha256 is not None:
            if self.activations is None:
                raise RuntimeError("pinned dispatch requires an artifact source")
            by_sha = getattr(self.activations, "artifact_by_sha", None)
            if not callable(by_sha):
                raise RuntimeError("artifact source does not support pinned dispatch")
            pinned = await by_sha(artifact_sha256)
            if pinned is not None:
                refs = [pinned]
        elif self.activations is not None:
            refs = await self.activations.ready_activations(event_type, hydrated)
        if playbook_ids is not None:
            admitted = frozenset(playbook_ids)
            refs = [ref for ref in refs if ref.playbook_id in admitted]

        selected: list[str] = []
        run_ids: list[str] = []
        deduplicated: list[str] = []
        commands: list[tuple[str, str, str]] = []
        traversals: list[DryRunTree] = []
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
                if not self._rule_selected(rule, event_type, hydrated):
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
            commands.extend(outcome.commands)
            if outcome.traversal is not None:
                traversals.append(outcome.traversal)

        return DispatchResult(
            dispatch_id=dispatch_id,
            rules_selected=tuple(selected),
            run_ids=tuple(run_ids),
            pending=tuple(pending),
            deduplicated=tuple(deduplicated),
            commands=tuple(commands),
            traversals=tuple(traversals),
        )

    async def dry_run(
        self,
        artifact_ref: ArtifactRef,
        event: Mapping[str, Any],
        principal: Any,
        *,
        invoke_ai: bool = False,
        max_paths: int = DEFAULT_MAX_SYMBOLIC_PATHS,
        max_step_visits: int = DEFAULT_DRY_RUN_MAX_STEP_VISITS,
    ) -> DryRunTree:
        """Traverse the executable artifact with a bounded symbolic work list.

        This intentionally does not call :meth:`run_rule`: dry-run has no
        durable run identity, and routing an unresolved branch through the
        live cursor would pause it after the first boundary.  It nevertheless
        uses the same artifact, trigger selection, value resolution,
        deterministic executors and transition fields as live execution.
        """
        artifact = self._load(artifact_ref)
        hydrated = await self._hydrate_event(event)
        event_type = self._event_type(hydrated)
        rules = tuple(
            rule for rule in artifact.rules if self._rule_selected(rule, event_type, hydrated)
        )
        dispatch_id = self._dispatch_id(hydrated)
        return await self._traverse_symbolic(
            artifact,
            artifact_ref,
            hydrated,
            principal,
            rules,
            mode=ExecutionMode.DRY_RUN,
            dispatch_id=dispatch_id,
            run_id_for=lambda rule_id: f"dry-run:{dispatch_id}:{rule_id}",
            invoke_ai=invoke_ai,
            max_paths=max_paths,
            max_step_visits=max_step_visits,
        )

    async def _traverse_symbolic(
        self,
        artifact: PlaybookDefinition,
        artifact_ref: ArtifactRef,
        event: Mapping[str, Any],
        principal: Any,
        rules: Sequence[Rule],
        *,
        mode: ExecutionMode,
        dispatch_id: str,
        run_id_for: Callable[[str], str],
        invoke_ai: bool = False,
        max_paths: int,
        max_step_visits: int,
        recorder: InMemoryRunRecorder | None = None,
    ) -> DryRunTree:
        """The one bounded symbolic walk shared by dry-run and shadow (§4.10).

        *mode* selects the executor table and nothing else: the trigger
        selection, value resolution, authorization routing, deterministic
        executors and transition fields are the live walk's.  A
        ``control=UNRESOLVED`` result forks the work list across the step's
        ``possible_outcomes`` instead of pausing a cursor, which is what lets
        shadow compare every downstream decision rather than stopping at the
        first external effect.

        In :attr:`ExecutionMode.SHADOW` every command the walk *would* have
        invoked is handed to *recorder* — the in-memory stand-in, never the
        repository — as its canonical ``(step_id, command, args)`` triple.
        """
        if mode is ExecutionMode.LIVE:
            raise ValueError("a symbolic traversal is dry-run or shadow, never live")
        if max_paths < 1 or max_step_visits < 1:
            raise ValueError("dry-run bounds must be >= 1")
        work: list[_DryRunCursor] = []
        paths: list[DryRunPath] = []
        omitted_frontiers: list[DryRunNode] = []
        truncated = False
        for rule in rules:
            scope = ResolutionScope(
                event=dict(event),
                context=self._context(artifact, rule, dispatch_id),
                bindings={},
                loop={},
            )
            if len(work) + len(paths) >= max_paths:
                # Keep the selected rule visible without manufacturing an
                # extra returned path beyond the global bound.
                omitted_frontiers.append(
                    DryRunNode(
                        rule.entry_step,
                        "unresolved",
                        reason="path_limit",
                        rule_id=rule.id,
                    )
                )
                truncated = True
            else:
                work.append(_DryRunCursor(rule.id, rule.entry_step, scope, None))

        visits = 0
        while work:
            cursor = work.pop(0)
            if visits >= max_step_visits:
                paths.append(
                    DryRunPath(
                        cursor.rule_id,
                        cursor.nodes
                        + (DryRunNode(cursor.step_id, "unresolved", reason="visit_limit"),),
                        "truncated",
                    )
                )
                truncated = True
                continue
            visits += 1
            step = artifact.steps.get(cursor.step_id)
            if step is None:
                raise UnknownStepType(f"step {cursor.step_id!r} is not in the artifact")
            try:
                inputs = {
                    name: resolve_value(value, cursor.scope)
                    for name, value in getattr(step, "inputs", {}).items()
                }
                authored_key = _authored_idempotency_key(step, cursor.scope)
            except ValueResolutionError as exc:
                paths.append(
                    DryRunPath(
                        cursor.rule_id,
                        cursor.nodes
                        + (
                            DryRunNode(
                                cursor.step_id, "unresolved", reason=exc.reason
                            ),
                        ),
                        "unresolved",
                    )
                )
                continue

            denied = isinstance(step, CommandStep) and not self._authorized(step, principal)
            if denied:
                # The live walk's stage 5.  Package 0 owns the decision and the
                # engine only routes it, so a denial is a resolved outcome the
                # artifact may declare an edge for — and the parity harness
                # must see the same edge live would take.
                result = ExecutorResult(
                    control=StepControl.ADVANCE,
                    outcome="unauthorized",
                    operation=f"command:{step.command}",
                    diagnostics=(f"capability denied: {step.command}",),
                )
            else:
                result = await self._execute_symbolic(
                    step, cursor, artifact, artifact_ref, principal, inputs,
                    authored_key=authored_key,
                    mode=mode,
                    dispatch_id=dispatch_id,
                    run_id=run_id_for(cursor.rule_id),
                    invoke_ai=invoke_ai,
                )

            if (
                mode is ExecutionMode.SHADOW
                and recorder is not None
                and isinstance(step, CommandStep)
                and result.recorded_command_args is not None
            ):
                recorder.record_command(cursor.step_id, step.command, result.recorded_command_args)

            # A foreach collection supplied by a symbolic upstream result has
            # no items to expand.  The live executor reports an input failure
            # because a concrete run cannot continue; the symbolic walk
            # instead reports the honest unresolved loop boundary and never
            # invents an empty collection or takes the failure edge.
            if isinstance(step, ForEachStep) and result.outcome == "input_resolution_failed":
                result = ExecutorResult(
                    control=StepControl.UNRESOLVED,
                    outcome="unavailable",
                    operation=result.operation,
                    diagnostics=("loop collection is unresolved",),
                )

            if result.control is StepControl.UNRESOLVED:
                reason = "; ".join(result.diagnostics) or "unresolved boundary"
                possible = result.possible_outcomes
                if not possible:
                    paths.append(
                        DryRunPath(
                            cursor.rule_id,
                            cursor.nodes
                            + (
                                DryRunNode(
                                    cursor.step_id, "unresolved", reason=reason
                                ),
                            ),
                            "unresolved",
                        )
                    )
                    continue
                available = max_paths - len(paths) - len(work)
                if len(possible) > available:
                    paths.append(
                        DryRunPath(
                            cursor.rule_id,
                            cursor.nodes
                            + (
                                DryRunNode(
                                    cursor.step_id,
                                    "unresolved",
                                    reason="path_limit",
                                    possible_outcomes=tuple(possible),
                                ),
                            ),
                            "truncated",
                        )
                    )
                    truncated = True
                    continue
                for outcome in possible:
                    target = getattr(step, "transitions", {}).get(outcome)
                    node = DryRunNode(
                        cursor.step_id,
                        "unresolved",
                        outcome=outcome,
                        target=target,
                        reason=reason,
                        possible_outcomes=tuple(possible),
                    )
                    if target is None:
                        paths.append(
                            DryRunPath(cursor.rule_id, cursor.nodes + (node,), "unresolved")
                        )
                    else:
                        work.append(
                            _DryRunCursor(
                                cursor.rule_id,
                                target,
                                cursor.scope,
                                cursor.loop,
                                cursor.nodes + (node,),
                                True,
                            )
                        )
                continue

            # A command node is ``simulated`` only when an adapter actually
            # answered for it; a routed denial is a deterministic decision.
            status = "simulated" if step.type == "command" and not denied else "resolved"
            node = DryRunNode(cursor.step_id, status, outcome=result.outcome)
            next_scope = cursor.scope
            if result.value is not None and getattr(step, "save_result_as", None):
                try:
                    next_scope = next_scope.with_binding(step.save_result_as, result.value)
                except ValueResolutionError as exc:
                    paths.append(
                        DryRunPath(
                            cursor.rule_id,
                            cursor.nodes
                            + (DryRunNode(cursor.step_id, "unresolved", reason=exc.reason),),
                            "unresolved",
                        )
                    )
                    continue
            next_loop = None if result.clear_loop else (result.loop_frame or cursor.loop)
            if result.control is StepControl.TERMINATE:
                paths.append(
                    DryRunPath(
                        cursor.rule_id,
                        cursor.nodes + (node,),
                        "unresolved" if cursor.unresolved else "resolved",
                        completed=not cursor.unresolved,
                    )
                )
                continue
            transitions: Mapping[str, str] = getattr(step, "transitions", {})
            if result.control is StepControl.GOTO:
                target = result.goto_step_id
            else:
                target = transitions.get(result.outcome)
                if target is None and result.outcome in ENGINE_RESERVED_OUTCOMES:
                    # The live walk's ``_advance_on_outcome`` fallback: only a
                    # reserved outcome may take the ``runtime_error`` edge.
                    target = transitions.get("runtime_error")
            if target is None:
                # The outcome is kept on the node: a parity reader needs to
                # see *which* decision had no edge (live fails the run there).
                paths.append(
                    DryRunPath(
                        cursor.rule_id,
                        cursor.nodes
                        + (
                            DryRunNode(
                                cursor.step_id,
                                "unresolved",
                                outcome=result.outcome,
                                reason="outcome has no transition",
                            ),
                        ),
                        "unresolved",
                    )
                )
                continue
            node = replace(node, target=target)
            work.append(
                _DryRunCursor(
                    cursor.rule_id,
                    target,
                    next_scope,
                    next_loop,
                    cursor.nodes + (node,),
                    cursor.unresolved,
                )
            )
        if truncated:
            # A bounded tree is an incomplete answer.  Even a path that
            # happened to reach a terminal before another frontier hit a
            # bound must not be reported as a completed dry-run result.
            paths = [replace(path, status="truncated", completed=False) for path in paths]
            if omitted_frontiers and paths:
                paths[0] = replace(paths[0], omitted_frontiers=tuple(omitted_frontiers))
        return DryRunTree(
            artifact_sha256=artifact_ref.artifact_sha256,
            rules_selected=tuple(rule.id for rule in rules),
            paths=tuple(paths),
            truncated=truncated,
            step_visits=visits,
        )

    async def _execute_symbolic(
        self,
        step: Any,
        cursor: _DryRunCursor,
        artifact: PlaybookDefinition,
        artifact_ref: ArtifactRef,
        principal: Any,
        inputs: Mapping[str, Any],
        *,
        authored_key: str | None = None,
        mode: ExecutionMode,
        dispatch_id: str,
        run_id: str,
        invoke_ai: bool,
    ) -> ExecutorResult:
        """One symbolic step attempt.  An executor error is an honest boundary."""
        executor_mode = (
            ExecutionMode.LIVE
            if invoke_ai and mode is ExecutionMode.DRY_RUN and step.type == "llm"
            else mode
        )
        ctx = StepContext(
            run_id=run_id,
            dispatch_id=dispatch_id,
            artifact_ref=artifact_ref,
            artifact=artifact,
            rule_id=cursor.rule_id,
            step_id=cursor.step_id,
            principal=principal,
            scope=cursor.scope,
            services=self.services,
            mode=mode,
            iteration_index=None if cursor.loop is None else cursor.loop.index,
            inputs=inputs,
            authored_idempotency_key=authored_key,
            loop_frame=cursor.loop,
        )
        try:
            return await executor_for(step.type, executor_mode).execute(step, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a preview/provider error is an honest boundary
            return ExecutorResult(
                control=StepControl.UNRESOLVED,
                outcome="runtime_error",
                diagnostics=(type(exc).__name__,),
                possible_outcomes=tuple(sorted(getattr(step, "transitions", {}))),
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
        project_task: bool = True,
    ) -> RunOutcome:
        """Create one run for *rule_id* and walk it.

        ``pause_before_start`` creates the run row and stops, which is what a
        caller wants when the run must exist before it may execute: the
        dependency-unavailable path (§4.13) and an operator starting a run
        for later resumption both need a durable, addressable run at its
        entry step rather than a promise to make one.
        """
        # Projection tasks are not yet created by the V2 engine.  Keeping the
        # caller-owned choice in the public contract makes assignment-routing
        # explicit and prevents a future projection layer from changing it.
        _ = project_task
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
            existing = await repository.find_run_for_dispatch(artifact.id, dispatch_id, rule.id)
            if existing is None:  # pragma: no cover - the index said it exists
                raise
            result_value = None
            list_receipts = getattr(repository, "list_receipts", None)
            if callable(list_receipts):
                receipts = await list_receipts(existing.run_id)
                result_value = next(
                    (
                        receipt.result["value"]
                        for receipt in reversed(receipts)
                        if "value" in receipt.result
                    ),
                    None,
                )
            return RunOutcome(
                existing.run_id,
                existing.lifecycle,
                "deduplicated",
                existing,
                result_value=result_value,
            )

        await self._emit(EVENT_RUN_STARTED, snapshot)
        if pause_before_start:
            return RunOutcome(snapshot.run_id, snapshot.lifecycle, "paused", snapshot)
        if mode is ExecutionMode.SHADOW:
            return await self._shadow_rule(snapshot, rule, artifact, artifact_ref, principal, repository)
        return await self._walk(snapshot, artifact, artifact_ref, principal, mode, repository)

    async def _shadow_rule(
        self,
        snapshot: RunSnapshot,
        rule: Rule,
        artifact: PlaybookDefinition,
        artifact_ref: ArtifactRef,
        principal: Any,
        recorder: InMemoryRunRecorder,
    ) -> RunOutcome:
        """A shadow run is a bounded symbolic traversal, not a cursor walk.

        Every shadow executor answers an external boundary with
        ``UNRESOLVED``; driving that through the live cursor pauses the run at
        its first command and compares nothing downstream.  Shadow therefore
        walks the same work list dry-run does — the same graph, the same
        resolution, the same transition fields — with the shadow executor
        table, forking across each boundary's possible outcomes and
        recording every command it would have issued.  Nothing here reaches
        a repository, a command, a preview adapter, a provider, a task, a
        gate or the bus: the recorder is in memory and ``_emit`` is a no-op
        outside live.

        The run finishes as soon as the traversal does.  Its outcome is
        ``completed`` only when every path reached a terminal without a
        symbolic fork on the way, ``truncated`` when a bound cut the tree,
        and ``unresolved`` otherwise — which is the honest answer for any
        graph with an external effect in it.
        """
        tree = await self._traverse_symbolic(
            artifact,
            artifact_ref,
            snapshot.event,
            principal,
            (rule,),
            mode=ExecutionMode.SHADOW,
            dispatch_id=snapshot.dispatch_id or "",
            run_id_for=lambda _rule_id: snapshot.run_id,
            max_paths=self.max_symbolic_paths,
            max_step_visits=self.max_symbolic_step_visits,
            recorder=recorder,
        )
        if tree.truncated:
            outcome = "truncated"
        elif tree.paths and all(path.completed for path in tree.paths):
            outcome = "completed"
        else:
            outcome = "unresolved"
        now = self.services.clock()
        final = replace(
            snapshot,
            lifecycle=RunLifecycle.COMPLETED,
            current_step_id=None,
            version=snapshot.version + 1,
            summary=f"shadow traversal: {outcome}",
            updated_at=now,
            completed_at=now,
        )
        recorder.snapshots[final.run_id] = final
        return RunOutcome(
            final.run_id,
            final.lifecycle,
            outcome,
            final,
            commands=tuple(recorder.commands),
            traversal=tree,
        )

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
        if snapshot.operator_decision is not None:
            return await self._resolve_operator_decision(
                snapshot, cause, principal, artifact, artifact_ref, mode, repository
            )
        snapshot, interrupted = await self._recover_interrupted_attempt(
            snapshot, artifact, artifact_ref, principal, repository
        )
        if interrupted is not None:
            if snapshot.lifecycle is RunLifecycle.PAUSED:
                return RunOutcome(snapshot.run_id, snapshot.lifecycle, interrupted, snapshot)
            # The interrupted receipt is a boundary of its own.  Re-load the
            # durable snapshot shape before replaying the identical attempt.
            snapshot = await repository.load_run(run_id) or snapshot
        if isinstance(cause, ChildTaskCompleted):
            return await self._resume_child_task(
                snapshot, cause, principal, artifact, artifact_ref, mode, repository
            )
        current_step = artifact.steps.get(snapshot.current_step_id or "")
        if snapshot.lifecycle is RunLifecycle.RUNNING and isinstance(current_step, LlmStep):
            step_id = snapshot.current_step_id or ""
            iteration = self._iteration_of(snapshot, step_id)
            attempt_number = self._next_attempt(snapshot, step_id, iteration)
            relevant = [
                int(turn.get("turn_index", -1))
                for turn in snapshot.llm_turns
                if turn.get("step_id") == step_id
                and int(turn.get("iteration", -1)) == iteration
                and int(turn.get("attempt", 1)) == attempt_number
            ]
            effective_principal = (
                await resolve_profile_principal(current_step, self.services, principal)
            ).principal
            attempt = _Attempt(
                snapshot=snapshot,
                step_id=step_id,
                step=current_step,
                started_at=self.services.clock(),
                # Do not attribute an ambiguous prior call to a broader
                # caller when the configured profile cannot be re-resolved.
                # A null projection is the fail-closed audit identity.
                principal=effective_principal,
                iteration=iteration,
                attempt=attempt_number,
                idempotency_key=idempotency_key(
                    snapshot.run_id,
                    step_id,
                    iteration,
                    attempt_number,
                ),
            )
            await self._commit_llm_turn(
                attempt,
                LLMToolTurn(
                    kind="interrupted",
                    turn_index=max(relevant, default=-1) + 1,
                    tool_call_ids=(),
                    results_digest=hashlib.sha256(b"[]").hexdigest(),
                    usage=TokenUsage(),
                ),
                artifact_ref,
                repository,
            )
            return RunOutcome(
                run_id,
                RunLifecycle.PAUSED,
                "operator_decision_required",
                attempt.snapshot,
                tuple(attempt.boundary_receipts),
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

    async def _recover_interrupted_attempt(
        self,
        snapshot: RunSnapshot,
        artifact: PlaybookDefinition,
        artifact_ref: ArtifactRef,
        principal: Any,
        repository: Any,
    ) -> tuple[RunSnapshot, str | None]:
        """Turn an incomplete durable attempt into a replay or an operator stop.

        An executor can have reached an external provider while its step
        boundary has not committed.  That is the only restart ambiguity the
        engine is allowed to infer; every other running snapshot simply
        resumes at its current step.
        """
        if snapshot.lifecycle is not RunLifecycle.RUNNING or snapshot.current_step_id is None:
            return snapshot, None
        step = artifact.steps.get(snapshot.current_step_id)
        if step is None:
            return snapshot, None
        if isinstance(step, LlmStep):
            # ``resume`` resolves an in-flight LLM attempt itself, with an
            # ``interrupted`` *turn* at the next turn index, so an operator
            # retry continues the same attempt from its durable transcript.
            return snapshot, None
        list_receipts = getattr(repository, "list_receipts", None)
        if not callable(list_receipts):
            return snapshot, None
        receipts = await list_receipts(snapshot.run_id)
        iteration = self._iteration_of(snapshot, snapshot.current_step_id)
        # An attempt is open when the latest receipt that speaks for the whole
        # attempt is still open: the ``attempt_start`` fence has no
        # ``completed_at``, and the ``step``/``interrupted``/resolution receipt
        # that closes it does.  Turn receipts neither open nor close one.
        latest_by_attempt: dict[int, StepReceipt] = {}
        for receipt in sorted(receipts, key=lambda item: item.snapshot_version):
            if (
                receipt.step_id == snapshot.current_step_id
                and receipt.iteration == iteration
                and receipt.receipt_kind in ATTEMPT_SCOPED_RECEIPT_KINDS
            ):
                latest_by_attempt[receipt.attempt] = receipt
        started = [
            receipt for receipt in latest_by_attempt.values() if receipt.completed_at is None
        ]
        marker = snapshot.context.get("_in_flight_attempt")
        if started:
            receipt = max(started, key=lambda item: item.attempt)
        elif isinstance(marker, Mapping) and marker.get("step_id") == snapshot.current_step_id:
            receipt = StepReceipt(
                receipt_id=uuid.uuid4().hex,
                run_id=snapshot.run_id,
                artifact_sha256=snapshot.artifact_sha256,
                rule_id=snapshot.rule_id,
                step_id=snapshot.current_step_id,
                step_kind=str(marker.get("step_kind") or "command"),
                outcome="skipped",
                started_at=float(marker.get("started_at") or self.services.clock()),
                snapshot_version=snapshot.version,
                iteration=int(marker.get("iteration", iteration)),
                attempt=int(marker.get("attempt", 1)),
                idempotency_key=str(marker.get("idempotency_key") or ""),
                completed_at=None,
            )
        else:
            return snapshot, None
        now = self.services.clock()
        decision_id = uuid.uuid4().hex
        # The next zero-based turn index.  Start receipts number starts, not
        # turns, so they stay out of this.
        turn_index = max(
            (
                item.turn_index
                for item in receipts
                if item.step_id == receipt.step_id
                and item.iteration == iteration
                and item.attempt == receipt.attempt
                and item.receipt_kind != "attempt_start"
            ),
            default=-1,
        ) + 1
        interrupted = replace(
            receipt,
            receipt_id=uuid.uuid4().hex,
            receipt_kind="interrupted",
            turn_index=turn_index,
            operator_decision_id=decision_id,
            outcome="failure",
            error="attempt interrupted before its boundary committed",
            error_code="interrupted",
            completed_at=now,
            duration_ms=max(0, int((now - receipt.started_at) * 1000)),
            snapshot_version=snapshot.version + 1,
        )
        if self._retry_safe_after_interruption(step):
            attempts = dict(snapshot.attempts)
            attempts[f"{receipt.step_id}:{iteration}"] = max(0, receipt.attempt - 1)
            replayable = replace(
                snapshot,
                attempts=attempts,
                context={
                    key: value
                    for key, value in snapshot.context.items()
                    if key != "_in_flight_attempt"
                },
                updated_at=now,
            )
            return await repository.commit_boundary(replayable, interrupted), "replay"

        attempts = dict(snapshot.attempts)
        attempts[f"{receipt.step_id}:{iteration}"] = receipt.attempt
        decision = OperatorDecision(
            step_id=receipt.step_id,
            attempt=receipt.attempt,
            reason="interrupted attempt may have reached an external side effect",
            raised_at=now,
            decision_id=decision_id,
            turn_index=turn_index,
        )
        paused = replace(
            snapshot,
            lifecycle=RunLifecycle.PAUSED,
            attempts=attempts,
            context={
                key: value
                for key, value in snapshot.context.items()
                if key != "_in_flight_attempt"
            },
            operator_decision=decision,
            error="operator_decision_required",
            error_code="operator_decision_required",
            updated_at=now,
        )
        operator_receipt = replace(
            interrupted,
            outcome="operator_decision_required",
            error=decision.reason,
            error_code="operator_decision_required",
        )
        return await repository.commit_boundary(paused, operator_receipt), "operator_decision_required"

    def _retry_safe_after_interruption(self, step: Any) -> bool:
        if not isinstance(step, CommandStep):
            return False
        registration = self.services.contracts.get(step.command)
        if registration is None:
            return False
        execution = registration.contract.execution
        return execution.retry_safe or execution.idempotency.mode in {"natural", "keyed"}

    async def _resolve_operator_decision(
        self,
        snapshot: RunSnapshot,
        cause: ResumeCause,
        principal: Any,
        artifact: PlaybookDefinition,
        artifact_ref: ArtifactRef,
        mode: ExecutionMode,
        repository: Any,
    ) -> RunOutcome:
        """Apply exactly one privileged, receipted ambiguity resolution (§4.8)."""
        if not isinstance(cause, OperatorResolution):
            return RunOutcome(snapshot.run_id, snapshot.lifecycle, "operator_decision_required", snapshot)
        policy = getattr(principal, "policy", None)
        authorized = (
            getattr(principal, "kind", None) is not PrincipalKind.PLAYBOOK
            and (
                not getattr(principal, "enforced", False)
                or (policy is not None and policy.allows("aq_commands", "playbook_admin"))
            )
        )
        if not authorized:
            return RunOutcome(snapshot.run_id, snapshot.lifecycle, "unauthorized", snapshot)
        decision = snapshot.operator_decision
        assert decision is not None  # narrowed by the caller
        if cause.kind not in {"accept", "accept_outcome", "retry", "fail", "cancel"}:
            return RunOutcome(
                snapshot.run_id, snapshot.lifecycle, "contract_violation", snapshot
            )
        action = "accept" if cause.kind == "accept_outcome" else cause.kind
        step = artifact.steps.get(decision.step_id)
        if step is None:
            return RunOutcome(snapshot.run_id, snapshot.lifecycle, "contract_violation", snapshot)
        now = self.services.clock()
        running = replace(
            snapshot,
            lifecycle=RunLifecycle.RUNNING,
            operator_decision=None,
            error=None,
            error_code=None,
            updated_at=now,
            context=dict(snapshot.context) | {"_operator_resolution": cause.kind},
        )
        resolution_receipt = StepReceipt(
            receipt_id=uuid.uuid4().hex,
            run_id=snapshot.run_id,
            artifact_sha256=snapshot.artifact_sha256,
            rule_id=snapshot.rule_id,
            step_id=decision.step_id,
            step_kind=step.type,
            receipt_kind="operator_decision",
            turn_index=decision.turn_index,
            operator_decision_id=decision.decision_id,
            outcome="success" if action in {"accept", "retry"} else "failure",
            started_at=now,
            snapshot_version=snapshot.version + 1,
            iteration=snapshot.loop.index if snapshot.loop else -1,
            attempt=decision.attempt,
            principal=self._principal_projection(principal),
            result={"operator_resolution": cause.kind},
            completed_at=now,
        )
        running = await repository.commit_boundary(running, resolution_receipt)
        if action == "retry":
            walked = await self._walk(running, artifact, artifact_ref, principal, mode, repository)
            return replace(
                walked,
                receipts=(resolution_receipt,) + walked.receipts,
            )
        attempt = _Attempt(
            snapshot=running,
            step_id=decision.step_id,
            step=step,
            started_at=now,
            principal=principal,
            iteration=self._iteration_of(running, decision.step_id),
            attempt=decision.attempt,
        )
        attempt.snapshot = replace(
            running,
            context={
                key: value
                for key, value in running.context.items()
                if key != "_operator_resolution"
            },
        )
        attempt.idempotency_key = idempotency_key(
            running.run_id, decision.step_id, attempt.iteration, attempt.attempt
        )
        attempt.receipt_result = {"operator_resolution": cause.kind}
        if action == "accept":
            attempt.outcome = str(cause.payload.get("outcome", ""))
            value = cause.payload.get("value")
            if value is not None and getattr(step, "save_result_as", None):
                try:
                    attempt.snapshot = bind_step_output(
                        attempt.snapshot,
                        step_id=step.save_result_as,
                        value=value,
                        declared=value.keys(),
                    )
                except (AttributeError, StateLimitExceeded):
                    attempt.outcome = "state_limit_exceeded"
                    attempt.error = "operator value exceeds the state limit"
            committed, receipt, outcome = await self._advance_on_outcome(
                attempt, artifact, artifact_ref, repository
            )
        elif action in {"fail", "cancel"}:
            attempt.outcome = "cancelled" if action == "cancel" else "runtime_error"
            attempt.lifecycle = (
                RunLifecycle.CANCELLED if action == "cancel" else RunLifecycle.FAILED
            )
            attempt.next_step_id = None
            committed, receipt, outcome = await self._commit(attempt, artifact_ref, repository)
        if receipt is not None:
            await self._emit(EVENT_STEP_COMPLETED, committed, step_id=receipt.step_id)
        if committed.is_terminal or committed.lifecycle is not RunLifecycle.RUNNING:
            await self._emit(EVENT_RUN_FINISHED, committed, outcome=outcome)
            return RunOutcome(
                committed.run_id,
                committed.lifecycle,
                outcome,
                committed,
                (resolution_receipt, receipt),
            )
        walked = await self._walk(committed, artifact, artifact_ref, principal, mode, repository)
        return replace(
            walked,
            receipts=(resolution_receipt, receipt) + walked.receipts
            if receipt
            else (resolution_receipt,) + walked.receipts,
        )

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
        """End a run, and mean it (§4.9).

        Four cases, and the enum is what separates them:

        * **terminal** — refused, in V1's own sentence, because "cancel a
          finished run" is a mistake worth naming rather than a no-op;
        * **paused** — immediate.  One boundary carries the ``cancelled``
          snapshot, its receipt and ``clear_run_waits``, so a durable wait
          cannot outlive the run it was suspending and claim it back after a
          restart;
        * **running with nothing in flight** — the intent is written and the
          walk's next boundary (§3.4 step 2) ends the run.  This is the half
          that stops a live run overwriting a cancellation, which is the
          failure ``playbook_commands.py:511-519`` documents about itself;
        * **running with an executor in flight** — ``cancelling``, exactly one
          ``request_cancel`` to a :class:`~...base.Cancellable` executor, and
          ``cancelled`` on acknowledgement or when the grace window expires,
          whichever comes first.

        The intent is durable *before* any of that, so every case survives a
        restart in the middle of it.
        """
        repository = self.runs
        if repository is None:
            raise RuntimeError("cancel requires a run repository")
        control = self._live.get(run_id)
        # Latch synchronously, before the first repository await.  A tool-turn
        # callback already in progress observes this even if its commit wins
        # the scheduler race with cancel's initial snapshot read.
        if control is not None and control.pending_cancel is None:
            control.pending_cancel = (principal, reason)
            control.cancel_event.set()
        snapshot = await repository.load_run(run_id)
        if snapshot is None:
            return RunOutcome(run_id, RunLifecycle.FAILED, "unknown_run")
        if control is not None and self._settled_cancellation(control) is not None:
            return await self._await_cancellation(control, principal, repository)
        if snapshot.is_terminal:
            return self._already_terminal(snapshot)

        async def persist_intent() -> RunOutcome | None:
            nonlocal snapshot
            while True:
                if snapshot.is_terminal:
                    return self._already_terminal(snapshot)
                if snapshot.lifecycle is RunLifecycle.PAUSED:
                    return await self._cancel_paused(
                        snapshot, repository, principal, reason
                    )
                # Already ``cancelling`` means another caller made the intent
                # durable.  Rewriting it would only advance the version under
                # the walk a second time.
                if snapshot.lifecycle is RunLifecycle.CANCELLING:
                    return None
                snapshot = await self._request_cancel_latest(
                    repository, snapshot, reason=reason, principal=principal
                )

        if control is None:
            completed = await persist_intent()
            if completed is not None:
                if completed.snapshot is not None:
                    await self._cancel_children(completed.snapshot, principal, cancel_children)
                return completed
            # No walk here.  Whichever process owns the run reads the intent
            # off the snapshot at its next boundary.
            await self._cancel_children(snapshot, principal, cancel_children)
            return RunOutcome(run_id, snapshot.lifecycle, "cancel_requested", snapshot)

        async with control.lock:
            if self._settled_cancellation(control) is None:
                completed = await persist_intent()
                if completed is not None:
                    if completed.snapshot is not None:
                        await self._cancel_children(
                            completed.snapshot, principal, cancel_children
                        )
                    return completed
                control.cancel_snapshot = snapshot
                await self._cancel_children(snapshot, principal, cancel_children)

                in_flight = control.in_flight
                if in_flight is None and not control.signalled:
                    return RunOutcome(
                        run_id, snapshot.lifecycle, "cancel_requested", snapshot
                    )
                if in_flight is not None and not control.signalled:
                    control.signalled = True
                    executor, step, ctx = in_flight
                    if isinstance(executor, Cancellable):
                        await executor.request_cancel(step, ctx)
        return await self._await_cancellation(control, principal, repository)

    async def _await_cancellation(
        self, control: _RunControl, principal: Any, repository: Any
    ) -> RunOutcome:
        """Wait out §4.9's grace window, then end the run either way.

        "Whichever first" is the whole point: an executor that acknowledges
        writes the boundary itself from inside the walk, and one that does not
        gets the boundary written *around* it here.  Both land on
        ``cancelled``; only the receipt says which happened.
        """
        try:
            await asyncio.wait_for(
                control.settled.wait(), timeout=self.cancellation_grace_seconds
            )
        except TimeoutError:
            await self._expire_grace(control, principal, repository)
        snapshot = control.final
        if snapshot is None:  # pragma: no cover - the two writers both set it
            snapshot = control.cancel_snapshot
        return RunOutcome(
            control.run_id,
            snapshot.lifecycle if snapshot else RunLifecycle.CANCELLED,
            "cancelled",
            snapshot,
            (control.receipt,) if control.receipt else (),
            cancellation=control.cancellation,
        )

    async def _expire_grace(
        self, control: _RunControl, principal: Any, repository: Any
    ) -> None:
        """End the run without the executor's cooperation.

        The step keeps running — the engine has no authority to kill work it
        did not start, and pretending otherwise is how a half-cancelled side
        effect gets attributed to nobody.  What it does own is the run: the
        boundary is written here, and the walk finds the run already settled
        when its executor eventually returns and writes nothing.
        """
        async with control.lock:
            if control.settled.is_set():
                return
            in_flight = control.in_flight
            base = control.cancel_snapshot
            if in_flight is None or base is None:  # pragma: no cover - defensive
                return
            _executor, step, ctx = in_flight
            attempt = self._cancellation_attempt(base, step, ctx.step_id, principal)
            await self._commit_cancellation(
                attempt,
                ctx.artifact_ref,
                repository,
                control,
                cancellation=CANCELLATION_GRACE_EXPIRED,
            )

    async def _cancel_paused(
        self, snapshot: RunSnapshot, repository: Any, principal: Any, reason: str
    ) -> RunOutcome:
        """§4.9's paused row: one boundary, one receipt, waits deregistered.

        The wait has to go in the *same* transaction as the terminal snapshot.
        A cancelled run whose ``playbook_waits`` row is still ``active`` is
        claimable by a later event, and after a restart nothing remembers that
        the run it points at is over.
        """
        try:
            artifact_ref = await self._ref_for(snapshot)
            artifact = self._load(artifact_ref)
            step_id = snapshot.current_step_id or ""
            step = artifact.steps[step_id]
        except (RuntimeError, KeyError, FileNotFoundError):
            # No artifact, no step kind, no receipt.  The run still has to
            # stop, and the repository's own cancel clears the waits, so the
            # degradation is the receipt rather than the cancellation.
            logger.warning(
                "V2 run %s cancelled without a receipt: its artifact is unreadable",
                snapshot.run_id,
            )
            updated = await self._request_cancel(
                repository, snapshot, reason=reason, principal=principal
            )
            return RunOutcome(snapshot.run_id, updated.lifecycle, "cancelled", updated)

        attempt = self._cancellation_attempt(snapshot, step, step_id, principal)
        attempt.wait_changes = WaitChangeSet(clear_run_waits=True)
        attempt.error = f"cancelled: {reason}"
        committed, receipt, _outcome = await self._commit_cancellation(
            attempt,
            artifact_ref,
            repository,
            None,
            cancellation=CANCELLATION_ACKNOWLEDGED,
        )
        return RunOutcome(
            snapshot.run_id,
            committed.lifecycle,
            "cancelled",
            committed,
            (receipt,) if receipt else (),
            cancellation=CANCELLATION_ACKNOWLEDGED,
        )

    @staticmethod
    async def _request_cancel(
        repository: Any, snapshot: RunSnapshot, *, reason: str, principal: Any
    ) -> RunSnapshot:
        return await repository.request_cancel(
            snapshot.run_id,
            expected_version=snapshot.version,
            reason=reason,
            requested_by=getattr(principal, "describe", lambda: "operator")(),
        )
    async def _request_cancel_latest(
        self,
        repository: Any,
        snapshot: RunSnapshot,
        *,
        reason: str,
        principal: Any,
    ) -> RunSnapshot:
        """Persist intent against the latest live version, or return its new state."""
        while snapshot.lifecycle is RunLifecycle.RUNNING:
            try:
                return await self._request_cancel(
                    repository, snapshot, reason=reason, principal=principal
                )
            except (SnapshotVersionConflict, IllegalLifecycleTransition):
                refreshed = await repository.load_run(snapshot.run_id)
                if refreshed is None:  # pragma: no cover - live runs are not deleted
                    raise RuntimeError(f"run {snapshot.run_id!r} disappeared")
                snapshot = refreshed
        return snapshot

    @staticmethod
    def _already_terminal(snapshot: RunSnapshot) -> RunOutcome:
        """V1's refusal, verbatim — ``playbook_commands.py:544``."""
        return RunOutcome(
            snapshot.run_id,
            snapshot.lifecycle,
            "already_terminal",
            snapshot,
            error=f"Run '{snapshot.run_id}' already {snapshot.lifecycle.value}",
        )

    def _cancellation_attempt(
        self, snapshot: RunSnapshot, step: Any, step_id: str, principal: Any
    ) -> _Attempt:
        iteration = self._iteration_of(snapshot, step_id)
        now = self.services.clock()
        return _Attempt(
            snapshot=replace(
                snapshot, cancel_requested_at=snapshot.cancel_requested_at or now
            ),
            step_id=step_id,
            step=step,
            started_at=now,
            principal=principal,
            iteration=iteration,
            attempt=self._next_attempt(snapshot, step_id, iteration),
        )

    async def _commit_cancellation(
        self,
        attempt: _Attempt,
        artifact_ref: ArtifactRef,
        repository: Any,
        control: _RunControl | None,
        *,
        cancellation: str,
    ) -> tuple[RunSnapshot, StepReceipt | None, str]:
        """The one boundary that ends a cancelled run."""
        attempt.outcome = "cancelled"
        attempt.lifecycle = RunLifecycle.CANCELLED
        attempt.cancelled_at = attempt.started_at
        attempt.cancellation = cancellation
        attempt.receipt_result = dict(attempt.receipt_result) | {
            CANCELLATION_KEY: cancellation
        }
        committed, receipt, outcome = await self._commit(attempt, artifact_ref, repository)
        if control is not None:
            control.final = committed
            control.receipt = receipt
            control.cancellation = cancellation
            control.settled.set()
        return committed, receipt, outcome

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
        control: _RunControl | None = None
        if mode is ExecutionMode.LIVE:
            # Only a live walk is cancellable: dry-run and shadow write
            # nothing, so there is nothing to stop and no repository to stop
            # it through.
            control = _RunControl(snapshot.run_id)
            self._live[snapshot.run_id] = control
        try:
            return await self._walk_steps(
                snapshot,
                artifact,
                artifact_ref,
                principal,
                mode,
                repository,
                receipts,
                visits,
                outcome,
            )
        finally:
            if control is not None and self._live.get(snapshot.run_id) is control:
                del self._live[snapshot.run_id]

    async def _walk_steps(
        self,
        snapshot: RunSnapshot,
        artifact: PlaybookDefinition,
        artifact_ref: ArtifactRef,
        principal: Any,
        mode: ExecutionMode,
        repository: Any,
        receipts: list[StepReceipt],
        visits: int,
        outcome: str,
    ) -> RunOutcome:
        max_step_visits = self._live_step_visit_limit(artifact, snapshot.rule_id)
        while not snapshot.is_terminal and snapshot.lifecycle is RunLifecycle.RUNNING:
            visits += 1
            if visits > max_step_visits:
                snapshot, receipt = await self._terminate(
                    snapshot, repository, "state_limit_exceeded", "step visit limit exceeded"
                )
                receipts.append(receipt)
                outcome = "state_limit_exceeded"
                break
            snapshot, step_receipts, outcome = await self._advance_one_step(
                snapshot, artifact, artifact_ref, principal, mode, repository
            )
            for receipt in step_receipts:
                receipts.append(receipt)
                if receipt.receipt_kind == "step":
                    await self._emit(EVENT_STEP_COMPLETED, snapshot, step_id=receipt.step_id)
            if snapshot.lifecycle is RunLifecycle.PAUSED and snapshot.pending_wait_claims:
                # Registration and inbox ingestion serialize per playbook, so
                # an event that arrived first is consumed *inside* the same
                # boundary that opened the wait and handed back on the
                # snapshot.  Sleeping on it would lose the only delivery.
                snapshot = replace(snapshot, lifecycle=RunLifecycle.RUNNING)
        await self._emit(EVENT_RUN_FINISHED, snapshot, outcome=outcome)
        result_value = next(
            (
                receipt.result["value"]
                for receipt in reversed(receipts)
                if "value" in receipt.result
            ),
            None,
        )
        return RunOutcome(
            snapshot.run_id,
            snapshot.lifecycle,
            outcome,
            snapshot,
            tuple(receipts),
            result_value=result_value,
        )

    @staticmethod
    def _live_step_visit_limit(artifact: PlaybookDefinition, rule_id: str) -> int:
        """Return a safety ceiling that admits every authored loop bound.

        Nested loops are invalid, so a conservative upper bound is every step
        once outside a loop plus every step once for each permitted iteration
        of every loop.  Exceeding that can only require revisiting a graph
        cycle beyond the explicit ForEach bounds.  The fixed floor preserves
        the pre-V2 safety allowance for non-loop graphs.
        """
        rule_steps = tuple(
            step for step in artifact.steps.values() if step.rule == rule_id
        )
        loop_iterations = sum(
            step.max_iterations
            for step in rule_steps
            if isinstance(step, ForEachStep)
        )
        return max(
            MIN_LIVE_STEP_VISITS,
            len(rule_steps) * (1 + loop_iterations),
        )

    async def _advance_one_step(
        self,
        snapshot: RunSnapshot,
        artifact: PlaybookDefinition,
        artifact_ref: ArtifactRef,
        principal: Any,
        mode: ExecutionMode,
        repository: Any,
    ) -> tuple[RunSnapshot, tuple[StepReceipt, ...], str]:
        """§3.4's eleven stages, in order.

        Stages 8-10 are the atomic unit: nothing between the executor call
        and the commit writes anything durable, so a crash in that window
        loses the attempt and never the run.
        """
        step_id = snapshot.current_step_id
        if step_id is None or step_id not in artifact.steps:
            failed, receipt, outcome = await self._fail_now(
                snapshot, repository, "contract_violation", f"step {step_id!r} is not in the artifact"
            )
            return failed, (() if receipt is None else (receipt,)), outcome
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
        resolution_kind = snapshot.context.get("_operator_resolution")
        if resolution_kind is not None:
            attempt.snapshot = replace(
                snapshot,
                context={
                    key: value
                    for key, value in snapshot.context.items()
                    if key != "_operator_resolution"
                },
            )

        async def finish(boundary: Any) -> tuple[RunSnapshot, tuple[StepReceipt, ...], str]:
            committed, receipt, boundary_outcome = await boundary
            receipts = list(attempt.boundary_receipts)
            if receipt is not None:
                receipts.append(receipt)
            return committed, tuple(receipts), boundary_outcome

        # 2. Cancellation — read from the snapshot the engine is about to
        #    write, so a live run cannot overwrite a cancellation.
        control = self._live.get(snapshot.run_id)
        settled = self._settled_cancellation(control)
        if settled is not None:
            committed, receipt, boundary_outcome = settled
            receipts = list(attempt.boundary_receipts)
            if receipt is not None:
                receipts.append(receipt)
            return committed, tuple(receipts), boundary_outcome
        cancelled = self._pending_cancellation(snapshot, control)
        if cancelled is not None:
            return await finish(
                self._commit_cancellation(
                    self._cancellation_attempt(cancelled, step, step_id, principal),
                    artifact_ref,
                    repository,
                    control,
                    cancellation=CANCELLATION_ACKNOWLEDGED,
                )
            )

        # 3. Deadline.
        if snapshot.deadline_at is not None and attempt.started_at >= snapshot.deadline_at:
            attempt.outcome = "timed_out"
            attempt.lifecycle = RunLifecycle.TIMED_OUT
            attempt.timed_out = True
            attempt.error = "run deadline fired"
            return await finish(self._commit(attempt, artifact_ref, repository))

        # A suspended run resumes *without* re-running its wait executor:
        # re-executing would compute a second correlation key and open a
        # second wait for one suspension.  The resumption is durable state
        # on the snapshot, so this branch is identical after a restart.
        if isinstance(step, WaitStep) and snapshot.wait is not None:
            return await finish(
                self._resume_wait(attempt, step, artifact, artifact_ref, repository)
            )

        scope = self._scope(snapshot, artifact)

        # 4. Resolve inputs.  A miss is an outcome *before* the executor
        #    runs; the engine never injects a marker and never coerces to "".
        try:
            inputs = {
                name: resolve_value(value, scope)
                for name, value in getattr(step, "inputs", {}).items()
            }
            authored_key = _authored_idempotency_key(step, scope)
        except ValueResolutionError as exc:
            attempt.outcome = "input_resolution_failed"
            attempt.error = exc.reason
            return await finish(
                self._advance_on_outcome(attempt, artifact, artifact_ref, repository)
            )

        # 5. Authorize.  Package 0 owns the decision; the engine only routes
        #    it, so an artifact can declare an edge for a denial.
        if isinstance(step, CommandStep) and not self._authorized(step, principal):
            attempt.outcome = "unauthorized"
            attempt.error = f"capability denied: {step.command}"
            return await finish(
                self._advance_on_outcome(attempt, artifact, artifact_ref, repository)
            )

        async def on_tool_turn(turn: LLMToolTurn) -> None:
            if control is None:
                await self._commit_llm_turn(attempt, turn, artifact_ref, repository)
                return
            async with control.lock:
                await self._commit_llm_turn(attempt, turn, artifact_ref, repository)
                if control.pending_cancel is not None:
                    cancel_principal, reason = control.pending_cancel
                    cancelling = await self._request_cancel_latest(
                        repository,
                        attempt.snapshot,
                        reason=reason,
                        principal=cancel_principal,
                    )
                    control.cancel_snapshot = cancelling
                    if cancelling.lifecycle is RunLifecycle.CANCELLING:
                        await self._commit_cancellation(
                            self._cancellation_attempt(
                                cancelling, step, step_id, cancel_principal
                            ),
                            artifact_ref,
                            repository,
                            control,
                            cancellation=CANCELLATION_ACKNOWLEDGED,
                        )
                    # Stop the client before it can issue a provider request
                    # beyond the completed and now durable turn.
                    raise asyncio.CancelledError

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
            cancel_event=control.cancel_event if control is not None else None,
            inputs=inputs,
            authored_idempotency_key=authored_key,
            loop_frame=snapshot.loop,
            llm_turns=snapshot.llm_turns,
            on_tool_turn=on_tool_turn if isinstance(step, LlmStep) else None,
        )

        # 6. Execute.  An unexpected exception is runtime_error carrying the
        #    exception *type*; a message can carry an argument value.
        executor = executor_for(step.type, mode)

        async def fence() -> tuple[RunSnapshot, tuple[StepReceipt, ...], str] | None:
            """Commit the receipted attempt-start boundary, or the failure."""
            nonlocal snapshot
            if not isinstance(step, EXTERNAL_STEP_TYPES):
                return None
            terminated = await self._commit_attempt_start(attempt, artifact_ref, repository)
            if terminated is not None:
                failed, error_receipt, boundary_outcome = terminated
                return (
                    failed,
                    tuple(attempt.boundary_receipts) + (error_receipt,),
                    boundary_outcome,
                )
            snapshot = attempt.snapshot
            return None

        if control is not None:
            # Cancellation latches before waiting for this lock.  Publishing
            # the durable marker and the in-process executor handle under the
            # same lock closes the gap where cancel could observe neither.
            async with control.lock:
                if control.pending_cancel is not None:
                    cancel_principal, reason = control.pending_cancel
                    current = await repository.load_run(snapshot.run_id) or snapshot
                    cancelling = await self._request_cancel_latest(
                        repository,
                        current,
                        reason=reason,
                        principal=cancel_principal,
                    )
                    control.cancel_snapshot = cancelling
                    return await finish(
                        self._commit_cancellation(
                            self._cancellation_attempt(
                                cancelling, step, step_id, cancel_principal
                            ),
                            artifact_ref,
                            repository,
                            control,
                            cancellation=CANCELLATION_ACKNOWLEDGED,
                        )
                    )
                fenced = await fence()
                if fenced is not None:
                    return fenced
                control.in_flight = (executor, step, ctx)
        else:
            fenced = await fence()
            if fenced is not None:
                return fenced
        try:
            result = await executor.execute(step, ctx)
        except asyncio.CancelledError:
            raise
        except LLMToolTurnBoundaryError as exc:
            cause = exc.__cause__
            if isinstance(cause, StateLimitExceeded):
                result = ExecutorResult(
                    control=StepControl.ADVANCE,
                    outcome="state_limit_exceeded",
                    diagnostics=(str(cause),),
                )
            elif isinstance(cause, (SnapshotVersionConflict, DuplicateAttempt)):
                current = await repository.load_run(snapshot.run_id)
                current = current or attempt.snapshot
                if current.lifecycle in {
                    RunLifecycle.CANCELLING,
                    RunLifecycle.CANCELLED,
                }:
                    if current.lifecycle is RunLifecycle.CANCELLED:
                        return (
                            current,
                            tuple(attempt.boundary_receipts),
                            "cancelled",
                        )
                    if control is not None:
                        control.cancel_snapshot = current
                        async with control.lock:
                            settled = self._settled_cancellation(control)
                            if settled is not None:
                                committed, receipt, boundary_outcome = settled
                            else:
                                cancellation_principal = (
                                    control.pending_cancel[0]
                                    if control.pending_cancel is not None
                                    else principal
                                )
                                # The model/tool turn completed before the
                                # cancellation request won the snapshot CAS.
                                # Rebase that completed boundary on the fresh
                                # cancelling row before writing the one
                                # terminal cancellation boundary.
                                attempt.snapshot = current
                                if (
                                    isinstance(cause, SnapshotVersionConflict)
                                    and exc.turn.kind != "interrupted"
                                ):
                                    await self._commit_llm_turn(
                                        attempt, exc.turn, artifact_ref, repository
                                    )
                                    current = attempt.snapshot
                                    control.cancel_snapshot = current
                                committed, receipt, boundary_outcome = (
                                    await self._commit_cancellation(
                                        self._cancellation_attempt(
                                            current,
                                            step,
                                            step_id,
                                            cancellation_principal,
                                        ),
                                        artifact_ref,
                                        repository,
                                        control,
                                        cancellation=CANCELLATION_ACKNOWLEDGED,
                                    )
                                )
                    else:
                        committed, receipt, boundary_outcome = (
                            await self._commit_cancellation(
                                self._cancellation_attempt(
                                    current, step, step_id, principal
                                ),
                                artifact_ref,
                                repository,
                                None,
                                cancellation=CANCELLATION_ACKNOWLEDGED,
                            )
                        )
                    receipts = list(attempt.boundary_receipts)
                    if receipt is not None:
                        receipts.append(receipt)
                    return committed, tuple(receipts), boundary_outcome
                if (
                    current.lifecycle is not RunLifecycle.RUNNING
                    or current.current_step_id != step_id
                ):
                    return current, tuple(attempt.boundary_receipts), "interrupted"
                failed, receipt = await self._terminate(
                    current,
                    repository,
                    "interrupted",
                    "another writer advanced this run",
                )
                return (
                    failed,
                    tuple(attempt.boundary_receipts) + (receipt,),
                    "interrupted",
                )
            else:
                result = ExecutorResult(
                    control=StepControl.ADVANCE,
                    outcome="runtime_error",
                    diagnostics=(type(cause).__name__,),
                )
        except Exception as exc:  # noqa: BLE001 - §3.4 step 6
            result = ExecutorResult(
                control=StepControl.ADVANCE,
                outcome="runtime_error",
                diagnostics=(type(exc).__name__,),
            )
        finally:
            if control is not None:
                control.in_flight = None

        # §4.9 — a cancellation that arrived while the step was in flight.
        # The result is not routed: the executor gave the run back, and this
        # boundary is the cancellation's rather than the step's.  Under the
        # control's lock, because ``cancel`` writes the same boundary when its
        # grace window expires first and exactly one of them may.
        if control is not None and (
            control.cancel_snapshot is not None or control.pending_cancel is not None
        ):
            async with control.lock:
                settled = self._settled_cancellation(control)
                if settled is not None:
                    committed, receipt, boundary_outcome = settled
                    receipts = list(attempt.boundary_receipts)
                    if receipt is not None:
                        receipts.append(receipt)
                    return committed, tuple(receipts), boundary_outcome
                cancellation_principal = principal
                if control.cancel_snapshot is None and control.pending_cancel is not None:
                    cancellation_principal, reason = control.pending_cancel
                    control.cancel_snapshot = await self._request_cancel_latest(
                        repository,
                        attempt.snapshot,
                        reason=reason,
                        principal=cancellation_principal,
                    )
                elif control.pending_cancel is not None:
                    cancellation_principal = control.pending_cancel[0]
                if control.cancel_snapshot is not None and control.cancel_snapshot.is_terminal:
                    return (
                        control.cancel_snapshot,
                        tuple(attempt.boundary_receipts),
                        "cancelled",
                    )
                cancelled = self._pending_cancellation(attempt.snapshot, control)
                if cancelled is not None:
                    return await finish(
                        self._commit_cancellation(
                            self._cancellation_attempt(
                                cancelled, step, step_id, cancellation_principal
                            ),
                            artifact_ref,
                            repository,
                            control,
                            cancellation=CANCELLATION_ACKNOWLEDGED,
                        )
                    )

        attempt.receipt_inputs = result.receipt_inputs
        attempt.receipt_result = dict(result.receipt_result)
        if resolution_kind is not None:
            attempt.receipt_result["operator_resolution"] = resolution_kind
        attempt.principal = result.effective_principal or attempt.principal
        attempt.idempotency_key = result.idempotency_key or attempt.idempotency_key
        attempt.usage = result.usage
        attempt.llm_calls = result.llm_calls
        if result.diagnostics:
            attempt.error = "; ".join(result.diagnostics)

        # 7. Validate the result.
        violation = self.validate_control(step, result)
        if violation is not None:
            attempt.outcome = violation
            attempt.error = f"{result.control} is not legal for a {step.type} step"
            return await finish(
                self._advance_on_outcome(attempt, artifact, artifact_ref, repository)
            )

        attempt.control = result.control
        attempt.outcome = result.outcome
        attempt.loop_frame = result.loop_frame
        attempt.clear_loop = result.clear_loop

        # 8. Bind, then the size checks.
        if result.value is not None and getattr(step, "save_result_as", None):
            try:
                attempt.snapshot = bind_step_output(
                    attempt.snapshot,
                    step_id=step.save_result_as,
                    value=result.value,
                    declared=result.value.keys()
                    if isinstance(result.value, Mapping)
                    else (),
                )
            except StateLimitExceeded:
                attempt.outcome = "state_limit_exceeded"
                attempt.error = "bound result exceeds the state limit"
                return await finish(
                    self._advance_on_outcome(attempt, artifact, artifact_ref, repository)
                )

        # 9 and 10.
        if result.control is StepControl.TERMINATE:
            if result.value is not None:
                attempt.receipt_result = dict(attempt.receipt_result) | {
                    "value": result.value
                }
            attempt.lifecycle = _TERMINAL_LIFECYCLE.get(
                result.terminal_outcome or "completed", RunLifecycle.COMPLETED
            )
            attempt.next_step_id = None
            return await finish(self._commit(attempt, artifact_ref, repository))
        if result.control is StepControl.GOTO:
            attempt.next_step_id = result.goto_step_id
            attempt.selected_transition = transition_id(
                snapshot.rule_id, step_id, result.outcome
            )
            return await finish(self._commit(attempt, artifact_ref, repository))
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
            return await finish(self._commit(attempt, artifact_ref, repository))
        if result.control is StepControl.UNRESOLVED:
            attempt.lifecycle = RunLifecycle.PAUSED
            attempt.error = attempt.error or "unresolved boundary"
            return await finish(self._commit(attempt, artifact_ref, repository))
        if result.control is StepControl.OPERATOR_DECISION:
            if (
                attempt.boundary_receipts
                and attempt.boundary_receipts[-1].receipt_kind == "interrupted"
            ):
                return (
                    attempt.snapshot,
                    tuple(attempt.boundary_receipts),
                    "operator_decision_required",
                )
            attempt.lifecycle = RunLifecycle.PAUSED
            attempt.outcome = "operator_decision_required"
            return await finish(self._commit(attempt, artifact_ref, repository))
        return await finish(
            self._advance_on_outcome(attempt, artifact, artifact_ref, repository)
        )

    async def _commit_attempt_start(
        self, attempt: _Attempt, artifact_ref: ArtifactRef, repository: Any
    ) -> tuple[RunSnapshot, StepReceipt, str] | None:
        """Fence external work behind a receipted boundary (§3.3.1).

        A ``CommandStep``, ``LlmStep`` or ``AgentTaskStep`` must not reach its
        first side effect without a durable record that the attempt began:
        a process killed between the two would otherwise leave nothing for
        recovery to see, and the attempt would replay as though it had never
        run.  The fence is an ordinary boundary — ``commit_boundary``
        advances the version and inserts one ``attempt_start`` receipt in the
        same transaction — so the run's history keeps one receipt per
        version.  The snapshot also carries the marker under
        ``context["_in_flight_attempt"]`` so an operator can see the open
        attempt without joining receipts; the attempt's next boundary clears
        it.

        Returns ``None`` when the attempt may proceed, or the terminal triple
        when another writer owns the run.
        """
        snapshot = attempt.snapshot
        marker = {
            "step_id": attempt.step_id,
            "step_kind": attempt.step.type,
            "iteration": attempt.iteration,
            "attempt": attempt.attempt,
            "started_at": attempt.started_at,
            "idempotency_key": attempt.idempotency_key,
        }
        next_snapshot = replace(
            snapshot,
            context=dict(snapshot.context) | {"_in_flight_attempt": marker},
            updated_at=self.services.clock(),
        )
        receipt = self._attempt_start_receipt(attempt, artifact_ref, ordinal=0)
        try:
            try:
                committed = await repository.commit_boundary(next_snapshot, receipt)
            except DuplicateAttempt:
                # This attempt identity has started before: a retry-safe
                # replay or an operator ``retry`` deliberately reuses the
                # attempt number so a keyed command sees the same key.  The
                # start receipt's ``turn_index`` is the ordinal of the start,
                # so the restart is receipted without inventing an attempt.
                # The failed insert rolled its version advance back, so the
                # same snapshot is still the right CAS expectation.
                receipt = self._attempt_start_receipt(
                    attempt,
                    artifact_ref,
                    ordinal=await self._next_start_ordinal(attempt, repository),
                )
                committed = await repository.commit_boundary(next_snapshot, receipt)
        except SnapshotVersionConflict:
            # As in ``_commit``: two writers at one boundary are two engines
            # that both think they own the run, and the write is never
            # retried.
            failed, error_receipt = await self._terminate(
                snapshot, repository, "interrupted", "another writer advanced this run"
            )
            return failed, error_receipt, "interrupted"
        attempt.snapshot = committed
        attempt.boundary_receipts.append(receipt)
        return None

    def _attempt_start_receipt(
        self, attempt: _Attempt, artifact_ref: ArtifactRef, *, ordinal: int
    ) -> StepReceipt:
        return StepReceipt(
            receipt_id=uuid.uuid4().hex,
            run_id=attempt.snapshot.run_id,
            artifact_sha256=artifact_ref.artifact_sha256,
            rule_id=attempt.snapshot.rule_id,
            step_id=attempt.step_id,
            step_kind=attempt.step.type,
            receipt_kind="attempt_start",
            turn_index=ordinal,
            outcome="started",
            started_at=attempt.started_at,
            # The version this boundary writes (see ``_commit``).
            snapshot_version=attempt.snapshot.version + 1,
            iteration=attempt.iteration,
            attempt=attempt.attempt,
            idempotency_key=attempt.idempotency_key,
            contract_fingerprint=self._contract_fingerprint(attempt.step),
            principal=self._principal_projection(attempt.principal),
            completed_at=None,
        )

    @staticmethod
    async def _next_start_ordinal(attempt: _Attempt, repository: Any) -> int:
        list_receipts = getattr(repository, "list_receipts", None)
        prior = await list_receipts(attempt.snapshot.run_id) if callable(list_receipts) else []
        return (
            max(
                (
                    receipt.turn_index
                    for receipt in prior
                    if receipt.receipt_kind == "attempt_start"
                    and receipt.step_id == attempt.step_id
                    and receipt.iteration == attempt.iteration
                    and receipt.attempt == attempt.attempt
                ),
                default=-1,
            )
            + 1
        )

    async def _commit_llm_turn(
        self,
        attempt: _Attempt,
        turn: LLMToolTurn,
        artifact_ref: ArtifactRef,
        repository: Any,
    ) -> None:
        """Persist one awaited client turn before provider execution continues."""
        snapshot = attempt.snapshot
        now = self.services.clock()
        iteration = attempt.iteration
        decision_id: str | None = None
        llm_turns = snapshot.llm_turns
        lifecycle = (
            RunLifecycle.CANCELLING
            if snapshot.lifecycle is RunLifecycle.CANCELLING
            else RunLifecycle.RUNNING
        )
        operator_decision = snapshot.operator_decision
        error = snapshot.error
        error_code = snapshot.error_code

        llm_turns = llm_turns + (
            {
                "kind": turn.kind,
                "step_id": attempt.step_id,
                "iteration": iteration,
                "attempt": attempt.attempt,
                "turn_index": turn.turn_index,
                "tool_call_ids": list(turn.tool_call_ids),
                "results_digest": turn.results_digest,
                "usage": {
                    "input_tokens": turn.usage.input_tokens,
                    "output_tokens": turn.usage.output_tokens,
                    "reported": turn.usage.reported,
                },
                "transcript_delta": [dict(message) for message in turn.transcript_delta],
            },
        )
        if turn.kind == "interrupted":
            decision_id = uuid.uuid4().hex
            lifecycle = RunLifecycle.PAUSED
            operator_decision = OperatorDecision(
                step_id=attempt.step_id,
                attempt=attempt.attempt,
                reason="LLM provider or tool call interrupted",
                raised_at=now,
                decision_id=decision_id,
                turn_index=turn.turn_index,
            )
            error = "LLM provider or tool call interrupted"
            error_code = "interrupted"

        budget = RunBudget(
            llm_calls=snapshot.budget.llm_calls + 1,
            total_tokens=snapshot.budget.total_tokens + turn.usage.total,
            max_total_tokens=snapshot.budget.max_total_tokens,
            cost_usd=snapshot.budget.cost_usd,
        )
        context = snapshot.context
        if turn.kind == "interrupted":
            # The interruption closes the attempt the fence opened; the
            # marker would otherwise outlive the ambiguity it exists to flag.
            context = {
                key: value for key, value in context.items() if key != "_in_flight_attempt"
            }
        next_snapshot = replace(
            snapshot,
            lifecycle=lifecycle,
            llm_turns=llm_turns,
            operator_decision=operator_decision,
            budget=budget,
            context=context,
            error=error,
            error_code=error_code,
            updated_at=now,
        )
        receipt = StepReceipt(
            receipt_id=uuid.uuid4().hex,
            run_id=snapshot.run_id,
            artifact_sha256=artifact_ref.artifact_sha256,
            rule_id=snapshot.rule_id,
            step_id=attempt.step_id,
            step_kind="llm",
            receipt_kind=turn.kind,
            turn_index=turn.turn_index,
            operator_decision_id=decision_id,
            outcome=(
                "operator_decision_required"
                if turn.kind == "interrupted"
                else "success"
            ),
            started_at=attempt.started_at,
            snapshot_version=snapshot.version + 1,
            iteration=iteration,
            attempt=attempt.attempt,
            idempotency_key=attempt.idempotency_key,
            principal=self._principal_projection(turn.principal or attempt.principal),
            result={
                "tool_call_ids": list(turn.tool_call_ids),
                "results_digest": turn.results_digest,
            },
            error=error if turn.kind == "interrupted" else None,
            error_code="interrupted" if turn.kind == "interrupted" else None,
            tokens_in=turn.usage.input_tokens,
            tokens_out=turn.usage.output_tokens,
            completed_at=now,
            duration_ms=max(0, int((now - attempt.started_at) * 1000)),
        )
        attempt.snapshot = await repository.commit_boundary(next_snapshot, receipt)
        attempt.boundary_receipts.append(receipt)
        await self._emit(
            EVENT_STEP_COMPLETED,
            attempt.snapshot,
            step_id=receipt.step_id,
            receipt_kind=receipt.receipt_kind,
            turn_index=receipt.turn_index,
        )

    @staticmethod
    def _pending_cancellation(
        snapshot: RunSnapshot, control: _RunControl | None
    ) -> RunSnapshot | None:
        """The snapshot this boundary must end the run from, or ``None``.

        Two sources, and the second is why ``cancel`` needs a handle at all.
        The snapshot's own ``cancel_requested_at`` covers a run that was
        already cancelled when the walk picked it up — after a restart, say.
        ``control.cancel_snapshot`` covers the intent recorded *during* this
        walk, and it is returned in preference because recording it advanced
        the run row: a boundary written against the version the walk still
        holds would lose the CAS and be reported as ``interrupted`` rather
        than as the cancellation it is.
        """
        if control is not None and control.cancel_snapshot is not None:
            fresh = control.cancel_snapshot
            # The walk's in-memory context (a resume overlay, say) is never
            # durable, so it is the version and the intent that are adopted,
            # not the whole row.
            return replace(
                snapshot,
                version=fresh.version,
                lifecycle=fresh.lifecycle,
                cancel_requested_at=fresh.cancel_requested_at,
            )
        if snapshot.cancel_requested_at is not None:
            return snapshot
        return None

    @staticmethod
    def _settled_cancellation(
        control: _RunControl | None,
    ) -> tuple[RunSnapshot, StepReceipt | None, str] | None:
        """``cancel`` already ended this run; the walk writes nothing more."""
        if control is None or not control.settled.is_set() or control.final is None:
            return None
        return control.final, None, "cancelled"

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
        terminal = attempt.lifecycle in TERMINAL_LIFECYCLES
        attempts = dict(snapshot.attempts)
        attempts[f"{attempt.step_id}:{attempt.iteration}"] = attempt.attempt
        agent_task_ids = snapshot.agent_task_ids
        if attempt.child_task_id and attempt.child_task_id not in agent_task_ids:
            agent_task_ids = agent_task_ids + (attempt.child_task_id,)
        budget = snapshot.budget
        if isinstance(attempt.step, LlmStep) and attempt.usage is not None:
            durable_turns = [
                turn
                for turn in snapshot.llm_turns
                if turn.get("step_id") == attempt.step_id
                and int(turn.get("iteration", -1)) == attempt.iteration
                and int(turn.get("attempt", 1)) == attempt.attempt
            ]
            durable_tokens = sum(
                int((turn.get("usage") or {}).get("input_tokens", 0))
                + int((turn.get("usage") or {}).get("output_tokens", 0))
                for turn in durable_turns
            )
            new_calls = max(0, attempt.llm_calls - len(durable_turns))
            new_tokens = (
                max(0, attempt.usage.total - durable_tokens) if new_calls else 0
            )
            budget = replace(
                budget,
                llm_calls=budget.llm_calls + new_calls,
                total_tokens=budget.total_tokens + new_tokens,
            )
        next_snapshot = replace(
            snapshot,
            lifecycle=attempt.lifecycle,
            agent_task_ids=agent_task_ids,
            current_step_id=attempt.next_step_id or attempt.step_id,
            wait=self._next_wait(attempt),
            loop=self._next_loop(attempt),
            attempts=attempts,
            context={
                key: value
                for key, value in snapshot.context.items()
                if key != "_in_flight_attempt"
            },
            budget=budget,
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
            tokens_in=attempt.usage.input_tokens if attempt.usage is not None else 0,
            tokens_out=attempt.usage.output_tokens if attempt.usage is not None else 0,
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
            failed = await repository.commit_boundary(failed, receipt)
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

        Sibling rule runs and matching playbooks share it, while a replay of
        the same event within one playbook collides on
        ``uq_playbook_v2_runs_dispatch_rule`` rather than on a pre-read that
        a concurrent dispatch could race.
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

    def _guard_admits(self, rule: Rule, event: Mapping[str, Any]) -> bool:
        """Evaluate the rule's delivery guard against the hydrated event.

        The guard is the typed lowering of V1's ``when`` clause, which
        ``src/orchestrator/core.py`` evaluated *before* starting a runner.  A
        rule whose guard is false was never dispatched under V1 and must not
        be selected here either: Package 6's parity harness compares
        ``rules_selected`` between the two arms, and selecting a
        guard-rejected rule is a behaviour change, not a reporting detail.

        The guard reads the event only — no bindings exist before the entry
        step — so a reference it cannot resolve means the guard is false, the
        same answer V1's ``truthy: false`` clause gave for a missing field.
        """
        if rule.guard is None:
            return True
        try:
            return evaluate_condition(rule.guard, ResolutionScope(event=event))
        except ValueResolutionError:
            return False

    def _rule_selected(
        self, rule: Rule, event_type: str, event: Mapping[str, Any]
    ) -> bool:
        """Subscription match *and* delivery guard — one selection answer."""
        return self._trigger_matches(rule, event_type, event) and self._guard_admits(rule, event)

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
        # ``core.py`` flags a pipeline-created review task as ``review_task``
        # right after hydrating, whatever the emitter sent, and every review
        # rule guards on it.  Omitting it here let a V2 review task's own
        # completion re-enter the review rule — the loop the flag exists to
        # stop.  Package 6's parity corpus is what surfaced it.
        task_dict = hydrated.get("task")
        if isinstance(task_dict, dict):
            flag_review_task_event(hydrated, task_dict.get("dedup_key"))
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
        if attempt.lifecycle in (RunLifecycle.FAILED, RunLifecycle.BLOCKED):
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
        by_sha = getattr(self.activations, "artifact_by_sha", None)
        if callable(by_sha):
            ref = await by_sha(snapshot.artifact_sha256)
            if ref is not None:
                return ref
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
        # Shadow and dry-run record only in memory.  In particular, a shadow
        # run may execute against production data, so an event is itself a
        # forbidden external side effect even when no command was invoked.
        if snapshot.mode != ExecutionMode.LIVE.value:
            return
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
