"""Level-triggered integration visits; policy and fenced actions are injected.

No daemon path is activated here. The adapter owner installs one reconciler at
the IntegrationService remote-pass boundary. Shadow is the default and visits
legacy subjects too; active visits only subjects owned by the reconciler.

A decision is committed before calling an adapter. Its result and due schedule
commit together. An interrupted visit is never blindly repeated: the next pass
records the ambiguity, advances the version and observes again. Remote-write
recovery belongs to the observer and the existing fenced, idempotent adapters.

The injected-call budget and the local commit budget are separate. A call that
times out still has to land its retry schedule, so the commit that carries it is
never cancelled by the remote budget that produced the timeout.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.event_bus import EventBus
from src.integration.subjects import (
    Decision,
    GateArgs,
    JournalKind,
    JournalMode,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    Subject,
    SubjectEngine,
    SubjectFacts,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    SubjectState,
    WaitArgs,
    schedule_values,
)

logger = logging.getLogger(__name__)

Observer = Callable[[Subject], Awaitable[SubjectFacts]]

# Identity, engine ownership, policy, bound and version are never policy outputs.
_TRANSITION_COLUMNS = frozenset(
    {
        "phase",
        "batch_id",
        "target_ref",
        "head_sha",
        "base_sha",
        "generation",
        "writer_status",
        "writer_task_id",
        "writer_fence_token",
        "writer_session_id",
        "writer_claimed_at",
        "writer_last_push_at",
        "writer_stop_proof",
        "budget_ordinal",
        "budget_class",
        "budget_started_at",
        "budget_deadline_at",
        "budget_attempts",
        "budget_attempt_limit",
    }
)


class VisitTransition(BaseModel):
    """The pinned table's outcome transition, validated before it reaches SQL.

    ``values`` contains domain columns (phase/head/writer/budget), never identity
    or ownership. The schedule retains the subject's pinned maximum wait.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    schedule: SubjectSchedule
    values: dict[str, Any] = Field(default_factory=dict)


class SubjectPolicy(Protocol):
    async def decide(self, subject: Subject, facts: SubjectFacts) -> Decision: ...

    async def settle(
        self, subject: Subject, decision: Decision, outcome: PrimitiveOutcome, *, now: float
    ) -> VisitTransition: ...


class CompiledSubjectPolicy(Protocol):
    """The pure evaluator supplied by the reviewed V2 policy compiler."""

    def evaluate(self, subject: Subject, facts: SubjectFacts) -> Decision: ...

    def schedule(
        self, subject: Subject, decision: Decision, outcome: PrimitiveOutcome, *, now: float
    ) -> SubjectSchedule: ...

    def phase(
        self, subject: Subject, decision: Decision, outcome: PrimitiveOutcome
    ) -> SubjectPhase | None: ...


class CompiledPolicyAdapter:
    """Bridge pure evaluate/schedule/phase to the visit ports without new policy.

    Every closed outcome uses the compiled artifact's route, including waits,
    unknowns and gate answers. A stricter policy wait bound clamps the due time
    while the subject retains the maximum it pinned at creation.
    """

    def __init__(self, policy: CompiledSubjectPolicy) -> None:
        self._policy = policy

    async def decide(self, subject: Subject, facts: SubjectFacts) -> Decision:
        return self._policy.evaluate(subject, facts)

    async def settle(
        self, subject: Subject, decision: Decision, outcome: PrimitiveOutcome, *, now: float
    ) -> VisitTransition:
        schedule = self._policy.schedule(subject, decision, outcome, now=now)
        if schedule.max_wait_seconds < subject.schedule.max_wait_seconds:
            schedule = SubjectSchedule(
                **{
                    **schedule.model_dump(),
                    "max_wait_seconds": subject.schedule.max_wait_seconds,
                }
            )
        phase = self._policy.phase(subject, decision, outcome)
        return VisitTransition(
            schedule=schedule, values={} if phase is None else {"phase": phase.value}
        )


class IntegrationReconciler:
    """One bounded, isolated page per pass; events only accelerate durable due times.

    Use ``tick(background=True)`` at a control-loop boundary: it returns while
    the single remote pass runs. ``tick()`` awaits that same pass for tests and
    manual reconciliation. Construction and ``start`` perform no engine transfer.

    Two budgets, because they buy different things: ``call_timeout_seconds``
    bounds the injected remote calls, whose cancellation the next due pass
    repairs, while ``bookkeeping_timeout_seconds`` bounds the local commit that
    makes a visit's outcome and schedule durable.
    """

    def __init__(
        self,
        db: Any,
        observer: Observer,
        policy: SubjectPolicy | CompiledSubjectPolicy,
        ports: PrimitivePorts,
        *,
        mode: JournalMode = JournalMode.SHADOW,
        kinds: Sequence[SubjectKind] = (SubjectKind.ROOT_BATCH,),
        page_size: int = 100,
        interval_seconds: float = 5.0,
        call_timeout_seconds: float = 180.0,
        bookkeeping_timeout_seconds: float = 60.0,
        backoff_seconds: float = 5.0,
        backoff_ceiling_seconds: float = 300.0,
        shadow_interval_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if page_size <= 0 or not kinds:
            raise ValueError("a reconciler needs a positive page size and subject kinds")
        timings = (
            interval_seconds,
            call_timeout_seconds,
            bookkeeping_timeout_seconds,
            backoff_seconds,
            backoff_ceiling_seconds,
            shadow_interval_seconds,
        )
        if any(value <= 0 for value in timings):
            raise ValueError("reconciler timeouts and intervals must be positive")
        self._db = db
        self._observer = observer
        self._policy = CompiledPolicyAdapter(policy) if hasattr(policy, "evaluate") else policy
        self._ports = ports
        self._mode = JournalMode(mode)
        self._kinds = tuple(SubjectKind(kind).value for kind in kinds)
        self._page_size = page_size
        self._interval = interval_seconds
        self._timeout = call_timeout_seconds
        self._bookkeeping = bookkeeping_timeout_seconds
        self._backoff = backoff_seconds
        self._backoff_ceiling = backoff_ceiling_seconds
        self._shadow_interval = shadow_interval_seconds
        self._clock = clock
        self._cursor: tuple[float, str] | None = None
        self._pass: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._visit_lock = asyncio.Lock()
        self._unsubscribers: list[Callable[[], None]] = []

    async def tick(self, now: float | None = None, *, background: bool = False) -> None:
        if self._stop.is_set():
            return
        if self._pass is None or self._pass.done():
            self._pass = asyncio.create_task(
                self._reconcile(self._clock() if now is None else now),
                name="integration-subject-reconciliation",
            )
        if not background:
            # A caller's cancellation must not spawn another concurrent pass.
            await asyncio.shield(self._pass)

    async def _reconcile(self, now: float) -> None:
        try:
            rows = await self._bounded(
                self._db.due_integration_subject_page(
                    now=now,
                    after=self._cursor,
                    limit=self._page_size,
                    kinds=self._kinds,
                    engine=(
                        SubjectEngine.RECONCILER.value if self._mode is JournalMode.ACTIVE else None
                    ),
                )
            )
            self._cursor = (
                (rows[-1]["next_due_at"], rows[-1]["id"]) if len(rows) == self._page_size else None
            )
            for row in rows:
                try:
                    await self.visit(row["id"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A database outage can prevent bookkeeping. The original
                    # durable due row remains eligible when the service recovers.
                    logger.exception("integration subject %s visit failed", row["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            self._cursor = None
            logger.exception("integration subject due page failed")

    async def _bounded(self, operation: Awaitable[Any]) -> Any:
        # No call timeout: a slow observe/seal/build finishes instead of being
        # cancelled and restarted from scratch on the next visit.
        return await operation

    async def _committed(self, operation: Awaitable[Any]) -> Any:
        """Bound the local durable commit by its own budget, never the call one.

        ``call_timeout_seconds`` governs injected work whose cancellation the
        next pass repairs. The commit carries the outcome and the due schedule
        that a timed-out call just produced; cancelling it on the remote budget
        loses the retry and strands the subject in its pre-visit state.
        """
        return await asyncio.wait_for(operation, timeout=self._bookkeeping)

    def _eligible(self, subject: Subject, now: float) -> bool:
        due = subject.schedule.next_due_at
        return (
            subject.is_live
            and subject.kind.value in self._kinds
            and due is not None
            and due <= now
            and (self._mode is JournalMode.SHADOW or subject.engine is SubjectEngine.RECONCILER)
        )

    async def visit(self, subject_id: str) -> None:
        """Observe, decide, act once, then atomically journal and schedule the outcome.

        Call through ``tick`` to retain the one-pass boundary. The fresh read
        avoids using a stale page after a wake, a close or an ownership transfer.
        """
        async with self._visit_lock:
            await self._visit(subject_id)

    async def _visit(self, subject_id: str) -> None:
        started = self._clock()
        row = await self._bounded(self._db.get_integration_subject(subject_id))
        if row is None:
            return
        subject = Subject.from_row(row)
        if not self._eligible(subject, started):
            return
        decision: Decision | None = None
        outcome: PrimitiveOutcome | None = None
        try:
            facts = await self._bounded(self._observer(subject))
            if not isinstance(facts, SubjectFacts) or (
                facts.subject_id,
                facts.subject_version,
                facts.kind,
            ) != (subject.id, subject.version, subject.kind):
                raise ValueError("observer returned another subject or version")
            proposed = await self._bounded(self._policy.decide(subject, facts))
            self._validate_decision(subject, facts, proposed)
            decision = proposed
            prepared = await self._bounded(self._prepare(subject, facts, decision))
            if prepared is None:
                return  # a newer visit or ownership transfer won
            decision, created = prepared
            if not created:
                # The durable prewrite survived without a completed visit. A
                # fresh version must re-observe uncertain remote effects.
                outcome = PrimitiveOutcome.unknown(decision.primitive, "interrupted_visit")
                transition = self._retry(subject, "interrupted_visit")
            elif self._mode is JournalMode.SHADOW:
                transition = self._shadow_schedule(subject)
            else:
                current = await self._bounded(self._db.get_integration_subject(subject.id))
                if current is None or not self._same_visit(subject, current):
                    return
                if decision.primitive is Primitive.WAIT:
                    outcome = PrimitiveOutcome(primitive=Primitive.WAIT, outcome="waiting")
                elif decision.primitive is Primitive.RECORD_DECISION:
                    outcome = PrimitiveOutcome(
                        primitive=Primitive.RECORD_DECISION, outcome="recorded"
                    )
                else:
                    outcome = await self._bounded(self._ports.invoke(subject, decision.request))
                transition = await self._transition(subject, facts, decision, outcome)
            self._validate_transition(subject, transition)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning("integration subject %s: %s", subject.id, reason)
            if outcome is None:
                outcome = PrimitiveOutcome.unknown(
                    decision.primitive if decision else Primitive.OBSERVE_SUBJECT, reason
                )
            transition = self._retry(subject, reason)
        await self._committed(self._finish(subject, decision, outcome, transition, started))

    @staticmethod
    def _validate_decision(subject: Subject, facts: SubjectFacts, decision: Decision) -> None:
        if not isinstance(decision, Decision) or (
            decision.subject_id,
            decision.subject_version,
            decision.policy,
            decision.facts_digest,
        ) != (subject.id, subject.version, subject.policy, facts.digest()):
            raise ValueError("decision does not bind the observed subject and pinned policy")
        if facts.holds and decision.primitive is not Primitive.WAIT:
            raise ValueError("a human product hold permits only wait")
        if (
            facts.gate is not None
            and facts.gate.status == "open"
            and (decision.primitive not in {Primitive.WAIT, Primitive.GATE})
        ):
            raise ValueError("an unanswered human gate permits only wait or gate")

    def _same_visit(self, subject: Subject, row: Mapping[str, Any]) -> bool:
        return row["version"] == subject.version and row["engine"] == subject.engine.value

    def _journal(
        self,
        subject: Subject,
        kind: JournalKind,
        *,
        now: float,
        decision: Decision | None = None,
        outcome: PrimitiveOutcome | None = None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        visit_id = f"{self._mode.value}:{subject.version}"
        return {
            "subject_id": subject.id,
            "entry_kind": kind.value,
            "idempotency_key": f"{visit_id}:{kind.value}",
            "visit_id": visit_id,
            "mode": self._mode.value,
            "policy_artifact_sha256": subject.policy.artifact_sha256,
            "subject_version": subject.version,
            "phase": subject.phase.value,
            "head_sha": subject.head_sha,
            "generation": subject.generation,
            "rule": decision.rule if decision else None,
            "primitive": decision.primitive.value if decision else Primitive.OBSERVE_SUBJECT.value,
            "facts_digest": decision.facts_digest if decision else None,
            "outcome": outcome.outcome if outcome else None,
            "payload": payload,
            "recorded_at": now,
        }

    async def _prepare(
        self, subject: Subject, facts: SubjectFacts, decision: Decision
    ) -> tuple[Decision, bool] | None:
        async with self._db.immediate() as conn:
            row = await self._db.lock_integration_subject_on(conn, subject.id)
            if row is None or not self._same_visit(subject, row):
                return None
            journal, created = await self._db.append_integration_subject_journal_on(
                conn,
                self._journal(
                    subject,
                    JournalKind.DECISION,
                    now=self._clock(),
                    decision=decision,
                    payload={
                        "decision": decision.model_dump(mode="json"),
                        "facts": facts.binding(),
                    },
                ),
            )
            return Decision.model_validate(journal["payload"]["decision"]), created

    def _retry(self, subject: Subject, reason: str) -> VisitTransition:
        now = self._clock()
        if subject.schedule.gate_id:
            schedule = SubjectSchedule.hold(
                now=now,
                gate_id=subject.schedule.gate_id,
                max_wait_seconds=subject.schedule.max_wait_seconds,
                revisit_at=now + min(self._backoff, subject.schedule.max_wait_seconds),
            )
        else:
            delay = SubjectSchedule.backoff(
                now=now,
                max_wait_seconds=subject.schedule.max_wait_seconds,
                refusal_streak=subject.schedule.refusal_streak,
                base_seconds=self._backoff,
                ceiling_seconds=self._backoff_ceiling,
            )
            schedule = SubjectSchedule(**{**delay.model_dump(), "wait_reason": reason})
        return VisitTransition(schedule=schedule)

    def _shadow_schedule(self, subject: Subject) -> VisitTransition:
        now = self._clock()
        bound = subject.schedule.max_wait_seconds
        if subject.schedule.gate_id:
            schedule = SubjectSchedule.hold(
                now=now,
                gate_id=subject.schedule.gate_id,
                max_wait_seconds=bound,
                revisit_at=now + min(self._shadow_interval, bound),
            )
        else:
            schedule = SubjectSchedule.wait(
                now=now,
                until=now + self._shadow_interval,
                reason="shadow_observation",
                max_wait_seconds=bound,
            )
        return VisitTransition(schedule=schedule)

    async def _transition(
        self, subject: Subject, facts: SubjectFacts, decision: Decision, outcome: PrimitiveOutcome
    ) -> VisitTransition:
        transition = await self._schedule_transition(subject, facts, decision, outcome)
        values = {} if outcome.is_unknown else outcome.detail.get("subject_values", {})
        if not isinstance(values, Mapping):
            raise ValueError("adapter subject_values must be a mapping")
        return VisitTransition(
            schedule=transition.schedule,
            values={**values, **transition.values},
        )

    async def _schedule_transition(
        self, subject: Subject, facts: SubjectFacts, decision: Decision, outcome: PrimitiveOutcome
    ) -> VisitTransition:
        now = self._clock()
        bound = subject.schedule.max_wait_seconds
        if isinstance(self._policy, CompiledPolicyAdapter):
            # Reuse the observer's proven deadline when the gate adapter did
            # not repeat it; the compiled route must never restart its clock.
            if (
                outcome.primitive is Primitive.GATE
                and outcome.outcome in {"created", "reused"}
                and "timeout_at" not in outcome.detail
                and facts.gate is not None
                and facts.gate.gate_id == outcome.detail.get("gate_id")
                and facts.gate.timeout_at is not None
            ):
                outcome = outcome.model_copy(
                    update={
                        "detail": {
                            **outcome.detail,
                            "timeout_at": facts.gate.timeout_at,
                        }
                    }
                )
            return await self._bounded(self._policy.settle(subject, decision, outcome, now=now))
        if outcome.is_unknown:
            return self._retry(subject, outcome.reason or "unknown")
        request = decision.request
        if isinstance(request, WaitArgs):
            if subject.schedule.gate_id:
                return VisitTransition(
                    schedule=SubjectSchedule.hold(
                        now=now,
                        gate_id=subject.schedule.gate_id,
                        max_wait_seconds=bound,
                        revisit_at=now + min(request.seconds, bound),
                    )
                )
            return VisitTransition(
                schedule=SubjectSchedule.wait(
                    now=now,
                    until=now + request.seconds,
                    reason=request.reason,
                    max_wait_seconds=bound,
                )
            )
        if isinstance(request, GateArgs) and outcome.outcome in {"created", "reused"}:
            gate_id = outcome.detail.get("gate_id")
            if not gate_id:
                return self._retry(subject, "gate outcome missing gate_id")
            deadline = outcome.detail.get("timeout_at")
            if deadline is None and facts.gate is not None and facts.gate.gate_id == gate_id:
                deadline = facts.gate.timeout_at
            if not request.no_default and deadline is None:
                if outcome.outcome == "created":
                    deadline = now + request.default_after_seconds
                else:
                    return self._retry(subject, "reused timed gate missing timeout_at")
            return VisitTransition(
                schedule=SubjectSchedule.hold(
                    now=now,
                    gate_id=gate_id,
                    max_wait_seconds=bound,
                    revisit_at=None if request.no_default else deadline,
                )
            )
        return await self._bounded(self._policy.settle(subject, decision, outcome, now=now))

    def _validate_transition(self, subject: Subject, transition: VisitTransition) -> None:
        if not isinstance(transition, VisitTransition):
            raise ValueError("policy must return VisitTransition")
        if set(transition.values) - _TRANSITION_COLUMNS:
            raise ValueError("policy attempted to change subject identity, ownership or schedule")
        if transition.schedule.max_wait_seconds != subject.schedule.max_wait_seconds:
            raise ValueError("policy attempted to change the pinned wait bound")
        if transition.schedule.due_set_at > self._clock():
            raise ValueError("policy returned a schedule based on a future clock")
        if transition.values.get("generation", subject.generation) < subject.generation:
            raise ValueError("policy attempted to decrease the subject generation")
        if (
            subject.batch_id is not None
            and transition.values.get("batch_id", subject.batch_id) != subject.batch_id
        ):
            raise ValueError("policy attempted to change a bound batch")
        if self._mode is JournalMode.SHADOW and (
            transition.values or transition.schedule.state is SubjectState.DONE
        ):
            raise ValueError("shadow may only record decisions and due bookkeeping")
        Subject.from_row(
            {
                **subject.to_row(),
                **transition.values,
                **schedule_values(transition.schedule),
            }
        )

    async def _finish(
        self,
        subject: Subject,
        decision: Decision | None,
        outcome: PrimitiveOutcome | None,
        transition: VisitTransition,
        started: float,
    ) -> None:
        now = self._clock()
        async with self._db.immediate() as conn:
            row = await self._db.lock_integration_subject_on(conn, subject.id)
            if row is None or not self._same_visit(subject, row):
                return
            if outcome is not None and self._mode is JournalMode.ACTIVE:
                await self._db.append_integration_subject_journal_on(
                    conn,
                    self._journal(
                        subject,
                        JournalKind.ACTION,
                        now=now,
                        decision=decision,
                        outcome=outcome,
                        payload={
                            "result": outcome.model_dump(mode="json"),
                            "transition": transition.model_dump(mode="json"),
                        },
                    ),
                )
            await self._db.update_integration_subject_on(
                conn,
                subject_id=subject.id,
                expected_version=subject.version,
                values={
                    **transition.values,
                    **schedule_values(transition.schedule),
                    "last_visit_at": now,
                },
                now=now,
                visit_started_at=started,
            )

    def subscribe(self, bus: EventBus) -> None:
        """Subscribe once; unknown/unrelated events are ignored, never actions."""
        if self._unsubscribers:
            raise ValueError("reconciler is already subscribed")
        self._unsubscribers.append(bus.subscribe("*", self.on_event))

    async def on_event(self, data: dict[str, Any]) -> None:
        event_type = data.get("_event_type", "")
        if not event_type.startswith(("integration.", "delivery.", "task.", "gate.")):
            return

        def ids(*keys: str) -> tuple[str, ...]:
            return tuple(data[key] for key in keys if isinstance(data.get(key), str) and data[key])

        try:
            await self._bounded(
                self._db.wake_integration_subjects(
                    now=self._clock(),
                    subject_ids=ids("subject_id"),
                    task_ids=ids("task_id", "source_task_id", "target_task_id", "parent_task_id"),
                    writer_task_ids=ids("task_id", "writer_task_id", "delegate_task_id"),
                    batch_ids=ids("batch_id"),
                    gate_ids=ids("gate_id"),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("integration subject wake failed; durable due schedule retained")

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="integration-subject-loop")

    async def stop(self) -> None:
        self._stop.set()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        tasks = [task for task in (self._task, self._pass) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = self._pass = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.tick(background=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass


class ScopedIntegrationDB:
    """Select runtime-owned project modes before paging shared subject kinds."""

    def __init__(self, db, integration_modes):
        self.db, self.integration_modes = db, tuple(integration_modes)

    def __getattr__(self, name):
        return getattr(self.db, name)

    async def due_integration_subject_page(self, **kwargs):
        return await self.db.due_integration_subject_page(
            **{**kwargs, "integration_modes": self.integration_modes}
        )
