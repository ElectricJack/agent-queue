"""Reconcile desired session state against observed session state.

One ``tick()`` per orchestrator cycle (~5 s), zero LLM calls, deterministic.
The daemon does not *drive* agents — it observes them and converges.

``tick()`` runs a fixed order of steps, each isolated so a failure in
one does not skip the rest:

1. **Refresh observation** — who is actually alive.
2. **Drain-ack** — the explicit end of the completion protocol.
3. **Prepare timeout** — a pool claim stuck mid-preparation is released.
4. **Exit classifier** — dead process, task still open → typed verdict.
5. **Abandoned claim loop** — an idle worker that stopped claiming is
   recycled, behind a compare-and-set.
6. **Orphans** — the two ways row and task can disagree: a live session
   whose task is no longer open (kill it), and an open task whose session
   row is not live (release it).
7. **Idle stop intent** — a session that has nothing left to do is
   stopped after a bounded grace, so a harness that never exits on its own
   still gives its pool slot and worktree back.
8. **Stall ladder** — alive but silent: nudge → restart → quarantine.
9. **Named desired-state** — converge persistent sessions (start/sleep).
10. **Backstop** — ``stuck_timeout_seconds`` as the final net, not the
    primary defense.

The single most important rule in this module: **unknown is not dead.**  A
``PartialListError`` from a provider, or a failed secondary probe, defers
every destructive action for that prefix.  The classifier acts on positive
evidence of death and on nothing else.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from src.claim_file import read_claim_file, remove_claim_file_if_matches
from src.models import SessionRecord, TaskStatus
from src.pool_claims import (
    idle_pool_claim_loop_stalled,
    is_live_pool_claim_task_status,
    pool_claim_budget_exhausted,
    pool_claim_cap,
    pool_claim_loop_stall_seconds,
)
from src.sessions.context import harness_progress, store_lower_bound
from src.sessions.exit_classifier import (
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    ExitVerdict,
    Verdict,
    classify_exit,
)
from src.sessions.harness_registry import runs_cli
from src.sessions.opencode_store import OpenCodeActivity, resolve_liveness_store
from src.sessions.provider import (
    Cap,
    CapabilityUnsupported,
    NotSubmitted,
    NudgeDeferred,
    PartialListError,
    SessionHandle,
)
from src.sessions.provider_liveness import request_inflight
from src.sessions.usage_limit_screen import (
    USAGE_LIMIT_PEEK_LINES,
    UsageLimitScreen,
    detect_usage_limit_screen,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DRAIN_ACK_KEY",
    "AdoptReport",
    "SessionReconciler",
    "StalledDeferral",
    "stall_reminder",
]

#: Provider-side metadata key the agent's ``aq session drain-ack`` sets.
DRAIN_ACK_KEY = "AQ_DRAIN_ACK"

#: task_metadata keys the stall ladder keeps its rung state on.  Metadata,
#: not columns: per the work-graph metadata-first rule, a new per-task
#: concept starts as a key.
META_STALL_NUDGES = "stall_nudges"
META_STALL_LAST_ACTION = "stall_last_action_at"


def stall_reminder(task_id: str, minutes: int) -> str:
    """The stall ladder's nudge text, kept to two rows of an 80-column pane.

    The provider confirms a submit by finding the exact text in the composer,
    and Claude Code 2.1.286 in an 80x24 pool pane shows only the last 7 rows
    of taller input.  The previous reminder spelled out four commands and
    named the task id three times: 562 characters, 9 rows, for a 72-character
    repair id, so it could never be confirmed.  The id is named once;
    ``aq task close`` resolves the session's own task without it, and the
    close's ``next_step`` names ``drain-ack``.  Agent-question replay must
    still read this as machine input (``_MACHINE_STALL`` in
    ``src/sessions/questions.py``).
    """
    return f"No progress for {minutes} min on task {task_id}: `aq task close`, or keep working."


_LIVE_STATES = ("starting", "running", "draining")
#: Task statuses in which a session still holds the task's claim.
_HELD_CLAIM_STATUSES = (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)
#: Public alias — other modules ask "is this session row live?" too
#: (``_cmd_task_close`` decides whether verification feedback can be
#: handed back in place rather than reopening the task).
LIVE_SESSION_STATES = _LIVE_STATES


@dataclass(frozen=True)
class _NudgeOutcome:
    """What one nudge attempt did.

    Three states, and the middle one used to be a bare ``None``: *nothing was
    typed, and the composer was not at fault* — a human's draft, or input AQ
    would not risk touching.  ``deferral`` carries the refusal so a caller
    that can corroborate "no person is here" does not have to re-probe the
    pane to find out which case it was.
    """

    #: ``True`` delivered, ``False`` failed, ``None`` untouched input.
    delivered: bool | None
    #: The :class:`NudgeDeferred` behind ``delivered is None``.
    deferral: NudgeDeferred | None = None


class StalledDeferral(StrEnum):
    """What a refused nudge proves about the holder behind it.

    Three answers, because "the composer is busy" is not one fact and the
    difference decides whether the ladder may spend something irreplaceable
    (a pool holder's claim).
    """

    #: A person is at the composer, or the agent is demonstrably working.
    #: Spend nothing and say nothing: their draft is their work.
    HOLD = "hold"
    #: No draft is being typed, and AQ has no independent record that could
    #: confirm or deny progress — an OpenCode worker, whose conversation
    #: AQ cannot read at all.  Visible (event, warning, doctor) and never
    #: destructive: the next pass may have proof.
    REPORT = "report"
    #: No draft is being typed, and progress evidence independent of the
    #: composer shows none.  Spend the rung and climb.
    ESCALATE = "escalate"


#: How often one holder's unverifiable stall is re-announced, and therefore
#: how stale the screen reading quoted in that announcement may be.  It never
#: becomes destructive, so silence would be the only alternative and that is
#: what made the 2026-10-03 stall invisible in the first place.
_STALL_REPORT_INTERVAL_SECONDS = 900.0
#: Rows of pane tail hashed for the screen reading an announcement quotes.
#: Enough to hold the composer and the message area above it.
_SCREEN_PEEK_LINES = 40
#: Bound on the in-memory screen digests and last reports.
_PROGRESS_CACHE_MAX = 512
#: How often the harness's own store is re-read for one session whose pane is
#: still reporting activity.  The lease that decides is minutes, the store is
#: tens of gigabytes, and every reading is a fresh read-only connection.
_WEDGE_SCAN_SECONDS = 60.0


@dataclass
class AdoptReport:
    """Outcome of the boot-time adoption pass."""

    adopted: list[str] = field(default_factory=list)
    dead: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    #: Provider names running with no row at all.  Killed, not adopted —
    #: see :meth:`SessionReconciler.adopt_on_start`.
    unknown_live: list[str] = field(default_factory=list)
    #: The subset of :attr:`unknown_live` the provider actually stopped.
    unknown_killed: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.adopted) + len(self.dead)


class SessionReconciler:
    """The cascade step that owns session lifecycle.

    Constructed once by the orchestrator, which passes itself as
    *orchestrator* so every terminal verdict can run the same cleanup tail
    the happy path runs (``release_session_task_resources``).  Without that
    reference each verdict transitioned the task and stopped, leaving the
    agent BUSY and the workspace locked for good.
    """

    def __init__(
        self,
        db,
        config,
        providers,
        harnesses=None,
        spec_builder=None,
        bus=None,
        orchestrator=None,
        starter=None,
        epoch: str | None = None,
        transcript_base_dir=None,
    ):
        self.db = db
        self.config = config
        self.providers = providers
        self.harnesses = harnesses
        self.spec_builder = spec_builder
        self.bus = bus
        self.orchestrator = orchestrator
        #: Anything with ``async ensure_started(kind, target_id, project_id)``
        #: -- :class:`~src.messages.session_lens.SessionLens` in production.
        #: ``None`` disables up-convergence entirely, which is the correct
        #: behavior for a daemon with no message routing wired.
        self.starter = starter
        self.epoch = epoch or uuid.uuid4().hex[:12]
        #: Where harness transcripts live (``Path.home()`` in production).
        #: Threaded into ``resolve_reader`` so a test can point the stall
        #: ladder's liveness check at a temp directory, as the transcript
        #: watcher is pointed by its own ``base_dir``.
        self.transcript_base_dir = transcript_base_dir
        #: (session id, instance token) -> (last changed at, screen digest).
        #: Reporting context only, never a decision input: it exists so the
        #: announcement of an unmeasurable stall can say how long the pane has
        #: shown the same thing, which is the fact an operator has to judge it
        #: by.  Instance-token keyed, because a relaunched session inherits
        #: nothing from its predecessor's observations.  Bounded; pruned by age.
        self._screen_digests: dict[tuple[str, str], tuple[float, str]] = {}
        #: (session id, instance token) -> when its unverifiable stall was
        #: last announced, so the announcement repeats instead of either
        #: vanishing after one line or repeating every tick.
        self._stall_reports: dict[tuple[str, str], float] = {}
        #: (session id, instance token) -> when its finished shape -- a
        #: recorded stop intent, or a spent claim budget -- was first
        #: observed.  The idle-stop grace runs from here, never from
        #: ``last_activity``: tmux's ``window_activity`` advances on any
        #: output, so an OpenCode pane at its final summary keeps that stamp
        #: fresh and no pane-derived idle bound can ever fire on one.
        #: Instance-token keyed, so a relaunched session starts its own
        #: clock.  Cleared when the row leaves the live set or the shape
        #: breaks; a daemon restart costs at most one more grace, which is
        #: why ``sessions.stop_intent_pending`` exists.
        self._idle_stop_intent_seen: dict[tuple[str, str], float] = {}
        #: (session id, instance token) -> (read at, the harness store's
        #: activity for that session, or ``None`` when unreadable).  The wedge
        #: check's own cache: a wedged TUI repaints forever, so without it the
        #: store would be re-read on every tick for every busy session.  Keyed
        #: like the screen digests, so a relaunched session inherits nothing.
        self._wedge_reads: dict[tuple[str, str], tuple[float, OpenCodeActivity | None]] = {}
        #: ``harness`` -> a reader of that CLI's own store, or ``None``.
        #: Resolved with this daemon's harness registry, so a harness that runs
        #: another CLI's executable (``opencode-zen``) finds the store its CLI
        #: writes.  One seam for both consumers of harness progress: the stall
        #: gate and :func:`~src.sessions.context.harness_progress`.
        self._liveness_store = lambda harness: resolve_liveness_store(
            harness, registry=self.harnesses
        )
        #: Names whose destructive handling is deferred this tick because
        #: enumeration was incomplete.  Cleared and rebuilt every tick.
        self._deferred_prefixes: set[str] = set()

    # -- helpers -----------------------------------------------------------

    @property
    def sessions_config(self):
        return self.config.sessions

    def _provider_for(self, session: SessionRecord):
        try:
            return self.providers.create(session.provider, self.config)
        except ValueError:
            logger.warning(
                "Session %s references unknown provider %r — leaving row untouched",
                session.id,
                session.provider,
            )
            return None

    @staticmethod
    def _handle(session: SessionRecord) -> SessionHandle:
        return SessionHandle(
            name=session.name,
            provider=session.provider,
            instance_token=session.instance_token,
        )

    async def _emit(self, event: str, **payload) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.emit(event, payload)
        except Exception:
            logger.debug("Event %s failed to emit", event, exc_info=True)

    def _process_names(self, session: SessionRecord) -> tuple[str, ...]:
        """Which process names count as "the agent" for this session.

        Sourced from the harness file, because a systemd-managed pane can
        move the agent into a child scope — matching on the pane's current
        command alone finds the shell, not the agent.
        """
        if self.harnesses is None:
            return ()
        harness = self.harnesses.get(session.harness, session.project_id)
        return harness.process_names if harness else ()

    async def _peek(self, provider, session: SessionRecord, lines: int = 40) -> str:
        if not provider.supports(Cap.PEEK):
            return ""
        try:
            return await provider.peek(self._handle(session), lines)
        except Exception:
            logger.debug("peek failed for session %s", session.id, exc_info=True)
            return ""

    # -- public API --------------------------------------------------------

    async def tick(self, *, now: float | None = None) -> None:
        """One reconciliation pass.  Never raises."""
        if not self.sessions_config.enabled:
            return
        now = now if now is not None else time.time()
        self._deferred_prefixes.clear()

        live = await self._step_observe(now)
        for step in (
            self._step_flock_audit,
            self._step_drain_ack,
            self._step_prepare_timeout,
            self._step_exits,
            self._step_abandoned_pool_claim_loop,
            self._step_orphans,
            self._step_idle_stop_intent,
            self._step_stall_ladder,
            self._step_named,
            self._step_backstop,
        ):
            try:
                await step(live, now)
            except Exception:
                logger.exception("Session reconciler step %s failed", step.__name__)

    async def _step_flock_audit(self, live, now: float) -> None:
        """Report hidden executions at least every thirty seconds."""
        if now - getattr(self, "_last_flock_audit", 0) < 30:
            return
        self._last_flock_audit = now
        from src.sessions.flock_audit import audit_flock

        findings = await audit_flock(self.db, self.providers, self.config)
        for finding in findings:
            logger.error("Flock invariant: %s", finding)

    async def adopt_on_start(self) -> AdoptReport:
        """Boot-time pass: re-bind live sessions, classify dead ones.

        A daemon restart must not abort in-flight work.  Live sessions keep
        their tasks IN_PROGRESS and get re-bound to this daemon's epoch;
        dead rows fall through to the exit classifier.

        Epoch is *provenance*, not a validity test — an older-epoch session
        is adoptable.  The instance token is what fences kills.
        """
        report = AdoptReport()
        if not self.sessions_config.enabled or not self.sessions_config.adopt_on_start:
            return report

        rows = await self.db.list_sessions(live_only=True)
        by_name = {r.name: r for r in rows}

        observed: dict[str, SessionHandle] = {}
        for prefix in ("s-", "n-", "p-"):
            provider = self.providers.create(self.sessions_config.provider, self.config)
            try:
                for handle in await provider.list_running(prefix):
                    observed[handle.name] = handle
            except PartialListError as exc:
                # Refuse to adopt *or* reap for this prefix.  A short list
                # read as authoritative is how a daemon kills its own live
                # agents on boot.
                logger.warning(
                    "Adoption: incomplete listing for prefix %r (%s) — deferring",
                    prefix,
                    exc,
                )
                report.deferred.append(prefix)
            except Exception:
                logger.exception("Adoption: list_running(%r) failed", prefix)
                report.deferred.append(prefix)

        for name, row in by_name.items():
            if any(name.startswith(p) for p in report.deferred):
                continue
            handle = observed.get(name)
            if handle is not None and handle.instance_token == row.instance_token:
                links = {}
                if row.agent_id is None:
                    if row.name == "n-supervisor--global" and row.project_id is None:
                        from src.agents.configuration import ensure_supervisor_agent
                        links["agent_id"] = (await ensure_supervisor_agent(self.db)).id
                    elif row.task_id:
                        task = await self.db.get_task(row.task_id)
                        if task and task.assigned_agent_id:
                            agent = await self.db.get_agent(task.assigned_agent_id)
                            if agent and agent.current_task_id == task.id:
                                links["agent_id"] = agent.id
                await self.db.update_session(row.id, epoch=self.epoch, **links)
                report.adopted.append(row.id)
                await self._emit(
                    "session.adopted",
                    session_id=row.id,
                    name=name,
                    task_id=row.task_id,
                    project_id=row.project_id,
                )
            else:
                report.dead.append(row.id)

        # Live sessions with no row at all come from a legacy/bypassed spawn.
        # New launches commit a registration before spawning. Design §8 asks for
        # "adopt if the markers match, else quarantine-kill"; with no row
        # there is nothing to match *against* — no task, no profile, no
        # instance token to fence on — so the reachable half is the kill.
        # Leaving them running is the worse failure: an unreachable agent
        # writing to a workspace the scheduler believes is free.
        for name, handle in observed.items():
            if name in by_name:
                continue
            report.unknown_live.append(name)
            provider = self.providers.create(self.sessions_config.provider, self.config)
            logger.warning(
                "Adoption: session %r is running with no row — killing (orphan)", name
            )
            try:
                await provider.stop(handle, grace=2.0)
                report.unknown_killed.append(name)
            except Exception:
                logger.exception("Adoption: could not stop orphan session %r", name)

        if report.total or report.unknown_live or report.deferred:
            logger.info(
                "Session adoption: %d adopted, %d dead, %d unknown-live, deferred=%s",
                len(report.adopted),
                len(report.dead),
                len(report.unknown_live),
                report.deferred or "none",
            )
        return report

    async def adopted_task_ids(self, report: AdoptReport) -> set[str]:
        """Task ids ``_recover_stale_state`` must skip, from *report*.

        The set is "tasks whose session we **confirmed alive**", not "tasks
        with a live row".  Those differ in four cases, and the difference is
        the highest-blast-radius failure in this module:

        * ``PartialListError`` deferred a whole prefix;
        * the observed handle's instance token did not match the row;
        * the provider raised while listing;
        * **always** with the shipped ``SubprocessProvider``, whose
          ``list_running`` is an in-memory dict that cannot see anything
          across a daemon restart.

        That last one is the real chain: adoption adopts nothing, yet every
        live row would be exempted anyway, so recovery leaves the task
        IN_PROGRESS, the agent BUSY, the workspace **locked** and the
        worktree cleanup skipped — while the detached OS process is still
        running and unreachable.  One tick later ``_step_exits`` calls it
        dead, RAPID_CRASH re-queues, and the scheduler launches a second
        agent into the worktree the first is still writing to.

        Deriving the set from ``report.adopted`` degrades that to the old
        blanket reset: correct if lossy, instead of protecting ghosts.
        """
        if not self.sessions_config.enabled or not report.adopted:
            return set()
        ids: set[str] = set()
        for session_id in report.adopted:
            row = await self.db.get_session(session_id)
            if row is not None and row.lifecycle in ("task", "pool") and row.task_id:
                ids.add(row.task_id)
        return ids

    # -- step 1: observation -----------------------------------------------

    async def _step_observe(self, now: float) -> list[SessionRecord]:
        """Return the live rows, refreshing ``last_activity`` from providers.

        The refreshed value is folded back into the returned records, not
        only written to the database.  Steps 4 and 6 measure idleness off
        these objects, and handing them the pre-refresh value would let a
        session that *just* showed activity be nudged in the very tick that
        observed it.
        """
        try:
            rows = await self.db.list_sessions(live_only=True)
        except Exception:
            logger.exception("Session reconciler: cannot list sessions")
            return []

        refreshed: list[SessionRecord] = []
        for row in rows:
            provider = self._provider_for(row)
            if provider is None or not provider.supports(Cap.ACTIVITY):
                refreshed.append(row)
                continue
            try:
                seen = await provider.last_activity(self._handle(row))
            except Exception:
                logger.debug("last_activity failed for %s", row.id, exc_info=True)
                refreshed.append(row)
                continue
            if seen and (row.last_activity is None or seen > row.last_activity):
                await self.db.touch_session_activity(row.id, seen)
                row = dataclasses.replace(row, last_activity=seen)
            refreshed.append(row)
        return refreshed

    # -- step 2: drain-ack -------------------------------------------------

    async def _step_drain_ack(self, live: list[SessionRecord], now: float) -> None:
        """Honour the second half of the completion protocol.

        ``aq task close`` transitions the task; ``aq session drain-ack``
        says "I am finished, you may kill me".  Both are required: an ack
        with the task still open is a *premature* drain and gets one nudge
        before being treated as an exit.
        """
        for row in live:
            if row.lifecycle == "pool":
                # Pool sessions never send a provider-side drain ack -- an
                # idle one marked stopped or sleeping is torn down the pool
                # way. A task pointer left from a released claim can name a
                # task already requeued or claimed by another worker; detach
                # it using the current assignment before deciding to wait.
                if row.desired_state not in ("stopped", "sleeping"):
                    continue
                if row.task_id is not None:
                    if not await self.db.release_displaced_pool_claim(row.id, now=now):
                        # Still bound to its task: wait for the close -- unless
                        # no close can come, because the held delegate is
                        # retired or the held task already closed and settled.
                        try:
                            await self._stop_drain_acked_holder(row)
                        except Exception:
                            logger.exception(
                                "Pool session %s: drain-acked holder stop failed", row.id
                            )
                        continue
                    row = await self.db.get_session(row.id)
                    if row is None:
                        continue
                if self.orchestrator is None:
                    logger.warning(
                        "Pool session %s wants draining but no orchestrator is wired "
                        "— skipping", row.id,
                    )
                    continue
                reason = "sleeping" if row.desired_state == "sleeping" else "drained"
                await self.orchestrator._terminate_pool_session(row, reason=reason)
                continue
            provider = self._provider_for(row)
            if provider is None:
                continue
            try:
                ack = await provider.get_meta(self._handle(row), DRAIN_ACK_KEY)
            except Exception:
                logger.debug("get_meta failed for %s", row.id, exc_info=True)
                continue
            if ack != "1":
                continue

            task = await self.db.get_task(row.task_id) if row.task_id else None
            closed = task is None or task.status not in (
                TaskStatus.IN_PROGRESS,
                TaskStatus.ASSIGNED,
            )
            if not closed:
                await self._premature_drain(provider, row, task, now)
                continue

            await self._stop_session(provider, row, reason="drain_ack")
            await self._emit(
                "session.drain_acked",
                session_id=row.id,
                name=row.name,
                task_id=row.task_id,
                project_id=row.project_id,
            )

    async def _stop_drain_acked_holder(self, row: SessionRecord) -> bool:
        """Stop a drain-acked pool worker whose held task no close can change.

        Each row takes exactly one proof -- a retired delegate's, else a
        settled claim's -- so an unconfirmed stop is retried next tick under
        the same one rather than repeated under the other.  The indexed
        database proofs come first: a worker drained over ordinary work
        reaches this branch every tick, and the provider read is a subprocess
        call.  Returns whether a stop was confirmed.
        """
        if self.orchestrator is None or row.task_id is None or row.desired_state != "stopped":
            return False
        retirement = await self.db.get_retired_integration_writer(row.task_id)
        if retirement is not None:
            return await self._stop_retired_delegate_writer(row, retirement)
        settlement = await self.db.get_settled_pool_claim(row.id)
        if settlement is not None and settlement["task_id"] == row.task_id:
            return await self._stop_settled_claim_holder(row, settlement)
        return False

    async def _stop_retired_delegate_writer(self, row: SessionRecord, retirement: dict) -> bool:
        """Stop a drain-acked pool worker whose held delegate can never close.

        The pool branch tears a worker down only once its held task is
        closed.  A delegate whose integration operation ended -- or whose
        stage expired after the operation moved on -- refuses every close and
        tells the worker to ``aq session drain-ack``, so that wait never ended:
        twice on 2026-09-30 a supervisor had to ``session kill`` the idle
        worker before the branch could be preserved or handed on
        (bold-impact-53).  Both of these must hold before anything is stopped:

        * the agent itself acknowledged its drain (the provider meta key), so
          it says its work is saved -- an operator drain or a pool scale-down
          writes ``desired_state`` but is not that statement; and
        * ``get_retired_integration_writer`` proves from durable state that no
          running operation can hand the seat back, human gates included.

        The teardown is ``_terminate_pool_session``, the path a supervisor
        kill reaches through the exit classifier: a confirmed stop comes
        first; an attached integration owner keeps the claim, checkout and
        session binding as the evidence owner recovery reads before it
        preserves the work and releases anything.  The task is never closed
        here and no pass is manufactured.  Returns whether the stop was
        confirmed; an unconfirmed one releases nothing and retries next tick.
        """
        if not await self._agent_acked_drain(row):
            return False
        logger.warning(
            "Pool session %s drain-acked holding retired integration delegate %s (%s) "
            "— stopping it",
            row.id,
            row.task_id,
            retirement["reason"],
        )
        if not await self._confirmed_pool_stop(row, reason="retired_delegate"):
            return False
        try:
            await self.db.add_task_comment(
                row.task_id,
                (
                    f"Session {row.id} ({row.name}) ran `aq session drain-ack` while "
                    f"still holding this task, and {retirement['reason']}: this delegate "
                    f"is retired ({retirement['disposition']}) and no close of it can be "
                    "accepted. The session reconciler stopped the session; the task was "
                    "not closed or marked passed. Its checkout and branch stay preserved: "
                    "owner recovery snapshots unpushed work to aq/preserved/<owner-row> "
                    "before it releases the branch."
                ),
                author_kind="supervisor",
                author_id="session-reconciler",
            )
        except Exception:
            logger.debug("could not record retired delegate stop for %s", row.task_id,
                         exc_info=True)
        await self._emit(
            "session.retired_delegate_stopped",
            session_id=row.id,
            name=row.name,
            task_id=row.task_id,
            project_id=row.project_id,
            retirement=retirement,
        )
        return True

    async def _stop_settled_claim_holder(self, row: SessionRecord, settlement: dict) -> bool:
        """Stop a drain-acked pool worker still bound to a task that already closed.

        ``aq task close`` commits the terminal transition before it hands the
        branch back and releases the claim.  A daemon restart in between left
        sound-orbit COMPLETED and delivered while its worker's attached branch
        owner kept ``release_displaced_pool_claim`` from detaching it: the
        worker acknowledged its drain and sat idle at its final summary, owner
        recovery answered ``writer_live``, and a supervisor had to kill it
        (wise-willow).  Both of these must hold before anything is stopped:

        * the agent itself acknowledged its drain (the provider meta key); and
        * ``get_settled_pool_claim`` proves from durable state that this
          session's own claim ended in a terminal task whose close or delivery
          is recorded, and that no running integration operation owns the task.

        No completion of the task may still be running in this daemon.
        ``complete_session_task`` commits the terminal transition, and with it
        the accepted-close marker the proof can rest on, and then runs the
        branch handoff under the task's control lock before the completion
        record is saved: an ack after an ambiguous (timed-out) close must not
        stop the worker mid-handoff.  After a restart nothing holds the lock.

        The teardown is the one ``_stop_retired_delegate_writer`` uses: a
        confirmed stop first; an attached owner keeps the claim, checkout and
        binding, so owner recovery can preserve unpushed work before it
        releases the branch.  The task's terminal state and completion are
        never touched.  Returns whether the stop was confirmed.
        """
        if self.orchestrator._task_control_held(row.task_id):
            return False
        if not await self._agent_acked_drain(row):
            return False
        logger.warning(
            "Pool session %s drain-acked still bound to settled task %s (%s) — stopping it",
            row.id,
            row.task_id,
            settlement["reason"],
        )
        if not await self._confirmed_pool_stop(row, reason="settled_claim"):
            return False
        try:
            await self.db.add_task_comment(
                row.task_id,
                (
                    f"Session {row.id} ({row.name}) ran `aq session drain-ack` while "
                    f"still bound to this task, and {settlement['reason']}: its close "
                    "committed but the claim was never released, so no further close can "
                    "come. The session reconciler stopped the session; the task's terminal "
                    "state and completion were not changed. Its claim, checkout and branch "
                    "binding stay for owner recovery (`aq integration release-owner`), "
                    "which snapshots unpushed work to aq/preserved/<owner-row> before it "
                    "releases the branch."
                ),
                author_kind="supervisor",
                author_id="session-reconciler",
            )
        except Exception:
            logger.debug("could not record settled claim stop for %s", row.task_id,
                         exc_info=True)
        await self._emit(
            "session.settled_claim_stopped",
            session_id=row.id,
            name=row.name,
            task_id=row.task_id,
            project_id=row.project_id,
            settlement=settlement,
        )
        return True

    async def _agent_acked_drain(self, row: SessionRecord) -> bool:
        """Whether the agent itself ran ``aq session drain-ack`` (the provider meta key).

        An operator drain or a pool scale-down writes ``desired_state`` too,
        but only the agent's own ack says its work is saved.
        """
        provider = self._provider_for(row)
        if provider is None:
            return False
        try:
            return await provider.get_meta(self._handle(row), DRAIN_ACK_KEY) == "1"
        except Exception:
            logger.debug("get_meta failed for %s", row.id, exc_info=True)
            return False

    async def _confirmed_pool_stop(self, row: SessionRecord, *, reason: str) -> bool:
        """``_terminate_pool_session``, then whether the stop is confirmed.

        An unconfirmed stop releases nothing and is retried next tick.
        """
        await self.orchestrator._terminate_pool_session(row, reason=reason)
        current = await self.db.get_session(row.id)
        if current is None or current.state != "stopped":
            logger.warning(
                "Pool session %s: %s stop unconfirmed; retrying", row.id, reason
            )
            return False
        return True

    async def _premature_drain(self, provider, row: SessionRecord, task, now: float) -> None:
        """Ack arrived with the task still open — nudge once, then classify."""
        logger.warning(
            "Session %s drain-acked with task %s still open — nudging",
            row.id,
            row.task_id,
        )
        await self._emit(
            "session.premature_drain",
            session_id=row.id,
            task_id=row.task_id,
            project_id=row.project_id,
        )
        outcome = await self._try_nudge(
            provider,
            row,
            f"You ran `aq session drain-ack` but task {row.task_id} is still open. "
            f"Close it first: `aq task close {row.task_id} --outcome ...`.",
        )
        if outcome.delivered:
            # Clear the ack so the next tick re-evaluates from scratch
            # rather than nudging on every cycle forever.
            try:
                await provider.set_meta(self._handle(row), DRAIN_ACK_KEY, "0")
            except Exception:
                logger.debug("could not clear drain ack on %s", row.id, exc_info=True)

    # -- pool: prepare-timeout ----------------------------------------------

    async def _step_prepare_timeout(
        self, live: list[SessionRecord], now: float
    ) -> None:
        """Release claims stuck in claiming/preparing (swarm-work-model §10.4).

        Pool-only: a task-lifecycle session has no ``claim_phase`` dance --
        its task is assigned at launch.  A pool session sits in
        ``claiming``/``preparing`` while it resolves and boots into a
        claimed task; a session that never leaves that window (crashed
        mid-prepare, workspace setup hung) would otherwise hold the task
        and the agent forever.

        Skipped entirely when ``swarm.enabled`` is false: pool sessions are
        only ever created by ``_reconcile_pools``, which is itself
        flag-gated, so the two ``list_sessions`` queries below can only
        come back empty.
        """
        if not getattr(self.config.swarm, "enabled", True):
            return
        timeout = self.config.swarm.prepare_timeout
        for phase in ("claiming", "preparing"):
            for s in await self.db.list_sessions(lifecycle="pool", claim_phase=phase):
                # A session with no ``claim_phase_at`` stamp at all is
                # treated as already stuck, not as fresh -- ``or 0.0``, not
                # ``or now``.
                if (s.claim_phase_at or 0.0) > now - timeout:
                    continue
                waiting, _ = await self._wait_lease(s, now)
                if waiting:
                    continue
                preparations = getattr(self.orchestrator, "claim_preparations", {})
                preparation = preparations.get((s.id, s.task_id, s.last_claim_epoch))
                if preparation is not None and not preparation.done():
                    # This daemon is still resetting the exact claim's
                    # workspace. Git operations have their own deadlines;
                    # releasing here races those writes and activation.
                    # Restarted daemons have no live request in this map,
                    # so abandoned preparations still expire normally.
                    continue
                released = await self.db.release_claim(
                    s.id,
                    task_status=TaskStatus.READY,
                    context="prepare_timeout",
                    now=now,
                    expected_task_id=s.task_id,
                    expected_claim_epoch=s.last_claim_epoch,
                    preparation_expired_before=now - timeout,
                    result="prepare_failed",
                    needs_attention="prepare_timeout",
                    prepare_backoff=True,
                )
                if not released.released:
                    continue
                if self.orchestrator is not None:
                    waiters = getattr(self.orchestrator, "claim_waiters", None) or {}
                    fut = waiters.pop((s.id, s.last_claim_epoch), None)
                    if fut is not None and not fut.done():
                        fut.set_result("prepare_failed")
                await self._emit(
                    "session.claim_timeout", session_id=s.id, task_id=s.task_id
                )

    # -- pool: abandoned claim loop ----------------------------------------

    def _pool_claim_loop_stall_seconds(self) -> float:
        """Grace before recycling an idle worker whose claim loop went quiet.

        Shared with pool supply accounting (:mod:`src.pool_claims`), so a
        worker stops counting as idle supply at the same moment it becomes
        eligible for recycling.
        """
        return pool_claim_loop_stall_seconds(self.config.swarm)

    async def _step_abandoned_pool_claim_loop(
        self, live: list[SessionRecord], now: float
    ) -> None:
        """Recycle an idle worker that stopped entering its claim loop.

        ``task_claim`` stamps session activity at entry, so an idle worker
        with no later claim (and no pane or transcript activity) for the
        bounded grace has no loop running.  Two shapes reach this: a worker
        that stopped after a released ``prepare_failed`` (which already left
        the task READY with its preparation backoff), and a worker that never
        reached its loop at all because the harness is parked on a provider
        screen — a usage limit or login prompt painted after startup, when
        the startup dialog pass is long over.  Either way the row, its agent
        and its workspace are held by a process that will not take work, and
        pool sizing no longer counts it as supply; tearing it down is what
        returns them.  The database CAS changes intent before teardown,
        closing the race with a late claim and preserving all session/instance
        fences.

        The usage-limit shape is also provider evidence, and this is the only
        place it is ever seen: the worker holds no task, so the stall ladder's
        limit-screen check never runs for it.  Before tearing down, the pane
        is read (:meth:`_idle_pool_usage_limit_screen`); on a match the recycle
        records the same ``exit_rate_limit`` evidence a death on a usage limit
        would, so two such workers trip the provider rather than pool sizing
        relaunching into the same limit indefinitely.  There is nothing to
        checkpoint or requeue.
        """
        if not getattr(self.config.swarm, "enabled", True) or self.orchestrator is None:
            return
        stall_seconds = self._pool_claim_loop_stall_seconds()
        stale_before = now - stall_seconds
        for observed in live:
            if (
                observed.lifecycle != "pool"
                or observed.state != "running"
                or observed.desired_state != "running"
                or observed.task_id is not None
                or observed.claim_phase is not None
            ):
                continue
            if not idle_pool_claim_loop_stalled(
                observed, now=now, stall_seconds=stall_seconds
            ) or self._is_deferred(observed.name):
                continue
            waiting, _ = await self._wait_lease(observed, now)
            if waiting:
                continue
            # Do not tear down based on an observation alone.  The guarded
            # update proves this exact running instance remains unclaimed;
            # a concurrent claim wins either the phase write or this intent
            # write, never both.
            fenced = await self.db.request_idle_pool_recycle(
                observed.id,
                instance_token=observed.instance_token,
                stale_before=stale_before,
            )
            if not fenced:
                continue
            current = await self.db.get_session(observed.id)
            if current is None or current.instance_token != observed.instance_token:
                continue
            reason = "claim_loop_stalled"
            screen = await self._idle_pool_usage_limit_screen(current)
            if screen is None:
                logger.warning(
                    "Pool session %s has not claimed for %.0fs (last result %s); recycling",
                    current.id,
                    stall_seconds,
                    current.last_claim_result or "none",
                )
            else:
                reason = "usage_limit_screen"
                logger.warning(
                    "Pool session %s has not claimed for %.0fs and is parked on a "
                    "usage-limit screen (%r); recording a rate-limit exit and recycling",
                    current.id,
                    stall_seconds,
                    screen.line,
                )
                availability = self._provider_availability()
                if availability is not None:
                    # Broad limit messages need corroboration. An explicit
                    # free-quota statement on a retry screen exhausts now.
                    await availability.record_rate_limit_exit(
                        current,
                        reason=f"usage-limit screen on an idle pool worker: {screen.line[:160]}",
                        usage_exhausted=screen.usage_exhausted,
                        resets_at=now + screen.retry_after if screen.retry_after else None,
                    )
            await self.orchestrator._terminate_pool_session(current, reason=reason)

    async def _idle_pool_usage_limit_screen(self, row: SessionRecord) -> UsageLimitScreen | None:
        """The limit line if an idle pool worker's pane is its usage-limit screen.

        Read after the recycle fence and before teardown, while the pane still
        exists.  Only where provider availability is tracked
        (``provider_failover.mode`` ``observe`` or ``enforce``): with it off
        there is nothing to record, and the recycle stays as it was.  The
        matcher is the stall ladder's own, deliberately strict
        (:func:`~src.sessions.usage_limit_screen.match_usage_limit_screen`).
        """
        failover = getattr(self.config, "provider_failover", None)
        if failover is None or not failover.tracking:
            return None
        provider = self._provider_for(row)
        if provider is None:
            return None
        return await self._usage_limit_screen(provider, row)

    def _runs_opencode(self, row: SessionRecord) -> bool:
        return runs_cli("opencode", row.harness, self.harnesses, row.project_id)

    async def _usage_limit_screen(self, provider, row: SessionRecord) -> UsageLimitScreen | None:
        return detect_usage_limit_screen(
            await self._peek(provider, row, USAGE_LIMIT_PEEK_LINES),
            opencode=self._runs_opencode(row),
        )

    # -- step 3: exits -----------------------------------------------------

    async def _step_exits(self, live: list[SessionRecord], now: float) -> None:
        for row in live:
            provider = self._provider_for(row)
            if provider is None:
                continue
            try:
                alive = await provider.process_alive(
                    self._handle(row), self._process_names(row)
                )
            except Exception:
                # A failed probe is not evidence of death.  Skip the row.
                logger.debug("process_alive probe failed for %s", row.id, exc_info=True)
                continue
            if alive:
                if row.task_id:
                    try:
                        await self._renew_live_session_leases(row, now)
                    except Exception:
                        logger.debug("liveness lease renewal failed for %s", row.id, exc_info=True)
                continue

            if row.state == "starting" and not await self._claim_starting_reconciliation(
                row, now
            ):
                # Enabled launches publish STARTING before provider.start.
                # A stale scan must wait on and re-read the same branch fence;
                # otherwise it can release the workspace under the writer.
                continue

            task = await self.db.get_task(row.task_id) if row.task_id else None
            peek = await self._peek(provider, row)
            if row.lifecycle == "pool":
                # session_kill persists stop intent before signalling, while
                # this tick may still hold the earlier live-session snapshot.
                # Read after both provider probes so requested stops drain
                # rather than quarantining the whole pool as rapid crashes.
                current = await self.db.get_session(row.id)
                if (
                    current is None
                    or current.state not in _LIVE_STATES
                    or current.instance_token != row.instance_token
                ):
                    continue
                row = current
            verdict = classify_exit(
                row,
                task,
                peek,
                now=now,
                rapid_crash_window=float(self.sessions_config.restart_window_seconds),
                opencode=self._runs_opencode(row),
            )
            # ``peek`` holds the whole pane plus scrollback: the hand-off
            # note's screen tail when the provider explains this death.
            await self._apply_verdict(provider, row, task, verdict, now, screen=peek)

    async def _renew_live_session_leases(self, row: SessionRecord, now: float) -> None:
        """Process existence grants only the first stall interval and backoff.

        HOLD/REPORT do not spend rungs, so the activity-based deadline is
        also necessary. A durable wait has its own finite deadline; question
        waits and drain intent do not grant process-only authority.
        """
        if row.state not in {"starting", "running"} or row.desired_state != "running":
            return
        if await self._waiting_for_question(row, now):
            return
        from src.integration.lock import DEFAULT_TTL_SECONDS, renew_session_leases_on_liveness

        waiting, resumed = await self._wait_lease(row, now)
        if waiting:
            task = await self.db.get_task(row.task_id)
            wait = await self.db.blocking_wait_for(row, task.claim_epoch, now) if task else None
            if wait is None:
                return
            renew_until = wait["deadline_at"]
        else:
            last = max(row.last_activity or row.started_at or 0.0, resumed)
            renew_until = (
                last
                + max(DEFAULT_TTL_SECONDS, float(self.sessions_config.lease_ttl_seconds))
                + float(self.sessions_config.stall_backoff_seconds)
            )
        async with self.db.immediate() as conn:
            await renew_session_leases_on_liveness(
                self.db, conn, row.id, now, renew_until=renew_until
            )

    async def _claim_starting_reconciliation(self, row: SessionRecord, now: float) -> bool:
        """Fence an enabled stale STARTING row, or defer a concurrent launch."""
        from src.sessions.launch import launch_in_progress

        current = await self.db.get_session(row.id)
        if current is None or current.state != "starting":
            return False
        task = await self.db.get_task(row.task_id) if row.task_id else None
        project = await self.db.get_project(row.project_id) if row.project_id else None
        if getattr(project, "hierarchical_integration_mode", "disabled") not in {
            "hierarchy",
            "train",
        }:
            return not launch_in_progress(self.db, row.id)
        repository_id = getattr(project, "integration_repository_id", None)
        if (
            task is None
            or not repository_id
            or task.repo_id != repository_id
            or not task.branch_name
        ):
            return False
        from src.integration.models import BranchKey, Fence
        from src.integration.ownership import BranchOwnership, BranchOwnershipError

        ownership = BranchOwnership(self.db)
        target = BranchKey(repository_id=repository_id, branch=task.branch_name)
        owner = await ownership.get_owner(target)
        if (
            owner is None
            or owner.get("owner_id") != task.id
            or owner.get("session_id") != row.id
            or not owner.get("workspace_id")
        ):
            return False
        fence = Fence(target=target, owner_id=task.id, token=int(owner["fence_token"]))
        try:
            return await ownership.begin_startup_reconciliation(
                fence,
                row.id,
                owner["workspace_id"],
                now=now,
            )
        except BranchOwnershipError:
            return False

    async def _apply_verdict(
        self,
        provider,
        row: SessionRecord,
        task,
        verdict: ExitVerdict,
        now: float,
        *,
        screen: str | None = None,
    ) -> None:
        await self._emit(
            "session.exited",
            session_id=row.id,
            name=row.name,
            task_id=row.task_id,
            project_id=row.project_id,
            verdict=str(verdict.verdict),
            reason=verdict.reason,
        )
        availability = self._provider_availability()
        if verdict.verdict is Verdict.RATE_LIMIT and availability is not None:
            # Broad pane text needs two sessions (D2/D3). The strict retry
            # screen can also carry an explicit quota statement and reset.
            await availability.record_rate_limit_exit(
                row,
                reason=verdict.reason,
                usage_exhausted=verdict.usage_exhausted,
                resets_at=verdict.resets_at,
            )

        if await self._apply_provider_failover(row, task, verdict, now, screen=screen):
            return

        if row.lifecycle == "pool":
            return await self._apply_pool_verdict(row, verdict, task, now)

        if verdict.verdict is Verdict.DRAINED:
            await self._stop_session(provider, row, reason="drained")
            return

        if verdict.verdict is Verdict.RATE_LIMIT:
            await self._apply_rate_limit_cooldown(row)
            if task is not None:
                await self.db.transition_task(
                    task.id,
                    TaskStatus.PAUSED,
                    context="rate_limit",
                    resume_after=now + verdict.cooldown_seconds,
                    assigned_agent_id=None,
                )
                await self._carry_resume_key(row, task)
                await self._release_task(task, row, reason="rate_limit")
                await self._emit(
                    "task.paused",
                    task_id=task.id,
                    project_id=task.project_id,
                    title=task.title,
                    reason="rate_limit",
                    resume_after=now + verdict.cooldown_seconds,
                )
            return

        if verdict.verdict is Verdict.RAPID_CRASH and self._provider_explains(row):
            # The provider is already unavailable: this death is its fault,
            # not the task's.  Spend no restart budget and quarantine nothing
            # (D13); launch suppression keeps the task from relaunching until
            # the provider is launchable again.
            await self.db.update_session(row.id, state="stopped", desired_state="stopped",
                                         ended_at=now, end_reason="rapid_crash")
            if task is not None:
                await self.db.transition_task(
                    task.id,
                    TaskStatus.PAUSED,
                    context="session_rapid_crash",
                    resume_after=now + self.sessions_config.restart_backoff_seconds,
                    assigned_agent_id=None,
                )
                await self._carry_resume_key(row, task)
                await self._release_task(task, row, reason="rapid_crash")
            return

        if verdict.verdict is Verdict.RAPID_CRASH:
            count = await self.db.bump_session_restarts(row.id)
            if count >= self.sessions_config.max_restarts:
                await self._quarantine(row, task, reason="rapid_crash", now=now)
                return
            await self.db.update_session(row.id, state="stopped", desired_state="stopped",
                                         ended_at=now, end_reason="rapid_crash")
            if task is not None:
                # Return to READY with a backoff so the normal scheduler
                # relaunches it.  The session row is history; the retry
                # produces a new one.
                await self.db.transition_task(
                    task.id,
                    TaskStatus.PAUSED,
                    context="session_rapid_crash",
                    resume_after=now
                    + self.sessions_config.restart_backoff_seconds * count,
                    assigned_agent_id=None,
                )
                await self._carry_resume_key(row, task)
                await self._release_task(task, row, reason="rapid_crash")
                await self._emit(
                    "task.restarted",
                    task_id=task.id,
                    project_id=task.project_id,
                    title=task.title,
                    attempt=count,
                    reason="rapid_crash",
                )
            return

        # PRODUCTIVE_DEATH — the agent worked, then vanished without
        # closing.  Never silently READY: the work may be half-done in the
        # worktree, so the exit is always recorded (``needs_attention``, an
        # INFO transition line and a durable task comment) before anything
        # else happens.
        #
        # BLOCKED is *not* the state for this.  BLOCKED means "a dependency
        # or a gate is holding this task", it is what ``aq task explain``
        # and the ready-frontier projection read that way, and it made a
        # recoverable worker exit look like a graph problem with no logged
        # reason at all.  A session that died with retry budget left is a
        # transient operational failure: PAUSED with a cooldown, exactly
        # like the rate-limit and rapid-crash legs, and the scheduler picks
        # it back up.  Only once the retry budget is spent does it become
        # BLOCKED — that is the leg the supervisor recovery incident
        # (``queue_task_recovery_notifications``) is for.
        await self.db.update_session(row.id, state="stopped", desired_state="stopped",
                                     ended_at=now, end_reason="session_exited_open")
        if task is not None:
            retries = task.retry_count or 0
            retriable = retries < (task.max_retries or 0)
            backoff = float(self.sessions_config.restart_backoff_seconds)
            logger.info(
                "Task %s: session %s exited without close (%s) — %s (retry %d/%d)",
                task.id,
                row.id,
                verdict.reason,
                (
                    f"PAUSED for {backoff:.0f}s, session_exited_without_close"
                    if retriable
                    else "BLOCKED, retry budget exhausted"
                ),
                retries,
                task.max_retries or 0,
            )
            await self.db.set_task_meta(task.id, "needs_attention", "session_exited_open")
            if retriable:
                await self.db.transition_task(
                    task.id,
                    TaskStatus.PAUSED,
                    context="session_exited_without_close",
                    resume_after=now + backoff,
                    retry_count=retries + 1,
                    assigned_agent_id=None,
                )
            else:
                await self.db.transition_task(
                    task.id,
                    TaskStatus.BLOCKED,
                    context="session_exited_without_close_exhausted",
                    assigned_agent_id=None,
                )
            await self._record_exit_incident(task, row, verdict, retriable, backoff)
            await self._carry_resume_key(row, task)
            await self._release_task(task, row, reason="session_exited_open")
            await self._emit(
                "task.needs_attention",
                task_id=task.id,
                project_id=task.project_id,
                title=task.title,
                session_id=row.id,
                reason=verdict.reason,
            )
            if not retriable:
                # Terminal: the session died without closing and there is no
                # retry left.  ``task.failed`` is what the reflection playbook
                # and the failure-notification path trigger on, so a task that
                # ends here has to raise it — otherwise the only exits that
                # ever reflect are the ones an agent was alive to report.
                await self._emit(
                    "task.failed",
                    task_id=task.id,
                    project_id=task.project_id,
                    title=task.title,
                    status=TaskStatus.BLOCKED.value,
                    context="session_exited_without_close_exhausted",
                    error=(
                        f"session {row.id} exited without close: {verdict.reason}; "
                        "retry budget exhausted"
                    ),
                )
            if retriable:
                await self._emit(
                    "task.restarted",
                    task_id=task.id,
                    project_id=task.project_id,
                    title=task.title,
                    session_id=row.id,
                    attempt=retries + 1,
                    reason="session_exited_without_close",
                )

    async def _apply_provider_failover(
        self,
        row: SessionRecord,
        task,
        verdict: ExitVerdict,
        now: float,
        *,
        screen: str | None = None,
    ) -> bool:
        """A mid-task death its provider explains (provider-failover D13).

        A ``RATE_LIMIT`` exit, or any death while the provider is unavailable
        (already, or tripped by this exit's evidence), in ``mode: enforce``.
        Such a death spends no retry, arms no pool-key quarantine and never
        pauses the task into the dead provider.  In this order:

        1. **Preserve the work** (``provider_failover_checkpoint``: WIP
           commit and push of the task's locked workspace) while the session
           row is still live -- a daemon that dies mid-push re-runs this on
           its next tick instead of the orphan sweep blocking an
           IN_PROGRESS task whose row went non-live first.
        2. The push failed -> **hold in place** (an operator pause with a
           local Git checkpoint): nothing is discarded to make a move
           possible.
        3. Otherwise release the claim's resources *before* the task becomes
           claimable, then the guarded transition: provider tripped ->
           READY for the ``provider-failover`` sweep; first, uncorroborated
           signal -> a ``launch.suspect_backoff_seconds`` pause with its
           ``provider_pause`` record.
        4. The hand-off note, once the outcome is written, quoting the tail
           of *screen* (the stopped session's last pane capture).

        Returns False, having done nothing, for every other exit, which keeps
        the verdict handling below exactly as it was.
        """
        from src.providers import inflight
        from src.providers.availability import EXIT_RATE_LIMIT

        orch = self.orchestrator
        availability = self._provider_availability()
        if (
            task is None
            or verdict.verdict is Verdict.DRAINED
            # A held claim only: a task paused, parked on a human or moved
            # meanwhile owns its own recovery, checkpoint included.
            or task.status not in _HELD_CLAIM_STATUSES
            or availability is None
            or orch is None
            or not hasattr(orch, "provider_failover_checkpoint")
        ):
            return False
        failure = inflight.ProviderFailure(
            kind=(
                EXIT_RATE_LIMIT if verdict.verdict is Verdict.RATE_LIMIT else inflight.SESSION_EXIT
            ),
            provider=availability.provider_for_harness(row.harness, row.project_id),
            harness=row.harness or "",
            profile_id=row.profile_id,
            session_id=row.id,
            detail=verdict.reason,
        )
        disposition = inflight.decide(availability, failure)
        if disposition == inflight.UNATTRIBUTED:
            return False
        pool = row.lifecycle == "pool"
        verdict_name = str(verdict.verdict)
        # A pool claim still being prepared: the daemon owns that slot.
        checkpoint = await orch.provider_failover_checkpoint(
            task, preserve=not pool or row.claim_phase == "active", reason=verdict.reason
        )

        async def handoff(outcome: str, *, held: bool = False) -> None:
            await orch.provider_failover_handoff(
                task,
                row,
                failure=failure,
                verdict=verdict_name,
                reason=verdict.reason,
                checkpoint=checkpoint,
                disposition=outcome,
                held=held,
                now=now,
                screen=screen,
            )

        if verdict.verdict is Verdict.RATE_LIMIT:
            await self._apply_rate_limit_cooldown(row)
        elif not pool:
            # A pool row stays live until ``_terminate_pool_session``
            # confirms the stop and releases its claim.
            await self.db.update_session(
                row.id, state="stopped", desired_state="stopped",
                ended_at=now, end_reason=verdict_name,
            )

        if checkpoint.at_risk:
            if not pool:
                await self._carry_resume_key(row, task)
            held = await orch.provider_failover_hold(
                task,
                reason=f"checkpoint {checkpoint.status}: {checkpoint.error or 'no remote carries it'}",
            )
            if not held:
                return False  # the ordinary verdict path still releases safely
            if pool:
                # Normally already stopped by the hold; otherwise this is
                # the confirmed-stop teardown every pool exit owes.
                await orch._terminate_pool_session(row, reason="provider_failover_hold")
            await handoff(disposition, held=True)
            await self._emit(
                "task.paused",
                task_id=task.id,
                project_id=task.project_id,
                title=task.title,
                reason=inflight.PUSH_FAILED_ATTENTION,
            )
            return True

        # Tripped goes straight back to the queue -- unless an integration
        # owner governs the workspace, whose release this path cannot see;
        # a short provider pause (which the sweep resumes at once) then
        # keeps the task unclaimable while that settles, as the launch path does.
        pause = disposition == inflight.SUSPECT or checkpoint.status == "integration_managed"
        if pause:
            context = (
                inflight.CONTEXT_SUSPECT
                if disposition == inflight.SUSPECT
                else inflight.CONTEXT_UNAVAILABLE
            )
            resume_after = now + float(availability.config.launch.suspect_backoff_seconds)
            meta = {
                inflight.PROVIDER_PAUSE_META: inflight.provider_pause_record(
                    availability, failure, context=context, resume_after=resume_after, now=now
                )
            }
        else:
            context, resume_after, meta = inflight.CONTEXT_UNAVAILABLE, None, {}

        if pool:
            if checkpoint.status in ("pushed", "clean"):
                await self._hand_back_pool_integration_owner(task, reason=context)
            if pause:
                await orch._terminate_pool_session(
                    row,
                    reason=context,
                    task_status=TaskStatus.PAUSED,
                    resume_after=resume_after,
                    task_meta=meta,
                )
            else:
                await orch._terminate_pool_session(row, reason=context)
            current = await self.db.get_task(task.id)
            moved = (
                current is not None
                and current.assigned_agent_id is None
                and current.status is (TaskStatus.PAUSED if pause else TaskStatus.READY)
            )
        else:
            # Release *before* the task is claimable again: the release frees
            # every workspace locked by this task id, and a task can be
            # claimed the moment it is written READY (or the sweep resumes a
            # provider pause).
            await self._carry_resume_key(row, task)
            await self._release_task(task, row, reason=context)
            moved = await self.db.transition_task_with_meta(
                task.id,
                TaskStatus.PAUSED if pause else TaskStatus.READY,
                meta=meta,
                context=context,
                from_statuses=_HELD_CLAIM_STATUSES,
                resume_after=resume_after,
                assigned_agent_id=None,
            )
        if not moved:
            # Someone else decided the task's fate meanwhile (an operator
            # pause or close), or the pool claim is retained for an
            # integration handoff; the work is preserved either way.
            logger.info(
                "Task %s: provider failover left the task as it found it (%s)",
                task.id,
                context,
            )
            return True
        await handoff(disposition)
        if pause:
            await self._emit(
                "task.paused",
                task_id=task.id,
                project_id=task.project_id,
                title=task.title,
                reason=context,
                resume_after=resume_after,
            )
        logger.info(
            "Task %s: session %s died on provider %s (%s) -- %s, no retry spent",
            task.id,
            row.id,
            failure.provider,
            verdict_name,
            f"paused until {resume_after:.0f}" if pause else "back in the queue for re-routing",
        )
        return True

    async def _hand_back_pool_integration_owner(self, task, *, reason: str) -> None:
        """Release a stopped pool writer's train/hierarchy owner before its teardown.

        An attached integration owner retains a pool claim through
        ``terminate_pool_session``, leaving the task to owner recovery.  The
        checkpoint just left the checkout clean and on ``origin`` -- the pool
        handoff's proof -- so hand the owner back now, as a failed claim
        preparation does; the teardown then releases the claim.  Refused or
        unmanaged, nothing changes and the claim stays retained.
        """
        release = getattr(self.orchestrator, "arelease_integration_writer_for_retry", None)
        if release is None:
            return
        try:
            await release(task, reason=reason, pool=True)
        except Exception:
            logger.warning(
                "Task %s: integration owner hand-back after provider failover failed",
                task.id,
                exc_info=True,
            )

    async def _record_exit_incident(
        self,
        task,
        row: SessionRecord,
        verdict: ExitVerdict,
        retriable: bool,
        backoff: float,
    ) -> None:
        """Leave a durable, readable record of an exit-without-close.

        The events above are ephemeral and the log line is not visible to
        the next worker.  A task comment is: ``aq task comments`` and the
        prime document both surface it, so whoever picks the task up next
        knows the previous attempt died mid-work rather than finding a
        half-finished worktree with no explanation.  Best-effort — an
        incident record must never break the reconciler tick.
        """
        disposition = (
            f"PAUSED for {backoff:.0f}s, then retried automatically"
            if retriable
            else "BLOCKED — retry budget exhausted, operator review required"
        )
        try:
            await self.db.add_task_comment(
                task.id,
                (
                    f"Session {row.id} ({row.name}) exited without calling "
                    f"`aq task close`: {verdict.reason}. "
                    f"needs_attention=session_exited_open. Task {disposition}. "
                    "Any work left in the worktree is still there; check the branch "
                    "before redoing it."
                ),
                author_kind="supervisor",
                author_id="session-reconciler",
            )
        except Exception:
            logger.debug(
                "could not record exit incident for %s", task.id, exc_info=True
            )

    async def _apply_rate_limit_cooldown(self, row: SessionRecord) -> None:
        """Sleep-state write shared by the task and pool RATE_LIMIT paths."""
        await self.db.update_session(
            row.id, state="sleeping", desired_state="sleeping", sleep_reason="rate_limit"
        )

    async def _apply_pool_verdict(
        self, row: SessionRecord, verdict: ExitVerdict, task, now: float
    ) -> None:
        """Pool sessions are never restarted in place.

        Every verdict ends in ``_terminate_pool_session``, which returns any
        held task to the frontier and starts a fresh session next tick.
        Rapid-crash and rate-limit also quarantine the pool key so the
        replacement does not launch straight back into the same failure.
        """
        orch = self.orchestrator
        if orch is None:
            logger.warning(
                "Pool session %s exited but no orchestrator is wired — skipping", row.id,
            )
            return
        if task is not None:
            note = {"RAPID_CRASH": "rapid_crash"}.get(verdict.verdict.name, "exited_holding_task")
            await self.db.set_task_meta(task.id, "needs_attention", note)
        # A rapid crash while the provider is already unavailable is the
        # provider's fault: pool sizing already targets zero for it, and a
        # per-(project, profile) quarantine on top would outlive its
        # recovery (provider-failover D13).
        if verdict.verdict is Verdict.RAPID_CRASH and not self._provider_explains(row):
            self._quarantine_pool_key(
                orch,
                row,
                until=now + self.sessions_config.restart_window_seconds,
                reason=f"rapid crash: {verdict.reason or 'session exited repeatedly'}",
            )
        elif verdict.verdict is Verdict.RATE_LIMIT:
            await self._apply_rate_limit_cooldown(row)
            # Task stays READY (the default ``_terminate_pool_session``
            # applies) -- it is the *pool key*, not this task, that is
            # rate-limited, so a different worker should pick it straight
            # back up.  The window matches whatever ``_step_exits`` handed
            # ``classify_exit`` for this verdict.
            self._quarantine_pool_key(
                orch,
                row,
                until=now + verdict.cooldown_seconds,
                reason=f"provider rate limit; retrying in {verdict.cooldown_seconds:.0f}s",
            )
        await orch._terminate_pool_session(row, reason=verdict.verdict.name.lower())

    def _provider_availability(self):
        return getattr(self.orchestrator, "provider_availability", None)

    def _provider_explains(self, row: SessionRecord) -> bool:
        """True when *row*'s provider is already unavailable (provider-failover D13)."""
        availability = self._provider_availability()
        if availability is None:
            return False
        provider = availability.provider_for_harness(row.harness, row.project_id)
        return availability.is_unavailable(provider)

    @staticmethod
    def _quarantine_pool_key(orch, row, *, until: float, reason: str) -> None:
        """Stop starting into this pool key until *until*, and say why.

        ``PoolsMixin._quarantine_pool`` owns the launch-failure window and
        always uses ``LAUNCH_BACKOFF``; an exit verdict carries its own
        (restart-window / provider-cooldown) deadline, so it writes the same
        two maps directly rather than borrowing that helper's fixed window.
        ``aq pool status`` reads both.
        """
        key = (row.project_id, row.profile_id)
        orch._pool_quarantine[key] = until
        reasons = getattr(orch, "_pool_quarantine_reason", None)
        if reasons is None:
            reasons = orch._pool_quarantine_reason = {}
        reasons[key] = reason
        logger.warning("pool %s/%s quarantined: %s", row.project_id, row.profile_id, reason)

    async def _waiting_for_question(self, row, now):
        service = getattr(self.orchestrator, "agent_questions", None)
        if service is None:
            return False
        return await service.is_waiting(row, now=now)

    async def _wait_lease(self, row, now) -> tuple[bool, float]:
        """Bounded dormancy and consumption grace without inventing activity.

        Use the task's current epoch even for task-lifecycle sessions whose
        last_claim_epoch is unset. The database additionally checks instance,
        ownership and task status. Death and explicit stops are never exempt.
        """
        if not row.task_id:
            return False, 0.0
        task = await self.db.get_task(row.task_id)
        if task is None:
            return False, 0.0
        wait = await self.db.agent_wait_for_claim(row, task.claim_epoch)
        if wait is None:
            return False, 0.0
        if await self.db.blocking_wait_for(row, task.claim_epoch, now):
            return True, 0.0
        # The global scan is bounded. Resolve this owner's overdue/completed
        # wait through the command boundary before evaluating a stale lease,
        # even when it fell outside that cycle's first 100 candidates.
        handler = getattr(self.orchestrator, "_command_handler", None)
        if handler is not None and wait["state"] == "active":
            from src.agent_waits import AgentWaitReconciler

            result = await AgentWaitReconciler(handler).tick(now=now, wait_id=wait["id"])
            if not result.get("success"):
                raise RuntimeError(f"wait reconciliation failed: {result}")
            wait = await self.db.agent_wait_for_claim(row, task.claim_epoch)
        resumed = wait["wait_resumed_at"] if wait else None
        if resumed is not None:
            last_action = float(
                await self.db.get_task_meta(row.task_id, META_STALL_LAST_ACTION) or 0.0
            )
            if last_action < resumed:
                await self.db.set_task_meta(row.task_id, META_STALL_NUDGES, "0")
                await self.db.set_task_meta(row.task_id, META_STALL_LAST_ACTION, str(resumed))
        return False, resumed or 0.0

    async def _screen_digest(self, provider, row: SessionRecord) -> str | None:
        """A digest of everything this session's pane is showing, read-only.

        ``None`` when the provider cannot be peeked at or the pane is gone.

        It is the pane's *tail*, not the composer box, and that is the whole
        point: an agent running one long tool leaves the composer untouched
        for minutes, so a fingerprint of the composer alone would read a
        working agent as a frozen screen.  The message area above it is where
        a working agent shows up.
        """
        if not provider.supports(Cap.PEEK):
            return None
        try:
            screen = await provider.peek(self._handle(row), _SCREEN_PEEK_LINES)
        except Exception:
            logger.debug("peek failed for %s", row.id, exc_info=True)
            return None
        if not screen:
            return None
        return hashlib.sha256(screen.encode("utf-8", "replace")).hexdigest()[:16]

    async def _screen_unchanged_seconds(
        self, provider, row: SessionRecord, *, now: float
    ) -> float | None:
        """How long this pane has shown the same thing, or ``None`` if unknown.

        Observation, not a verdict, and deliberately not a decision input.  It
        is quoted alongside an unmeasurable stall because a screen that has not
        moved for an hour is a different fact from one that changed a minute
        ago, and a human is the one who can tell a wedged TUI from an agent
        thinking.  A terminal read is not evidence about the agent's progress:
        an unchanged pane is what an agent between two writes looks like just
        as much as what a wedge looks like.

        The clock is the time the screen last *changed*, not the time it was
        last sampled, so a stall polled every few seconds is still measured in
        real seconds; and a failed read leaves the previous reading untouched
        instead of re-stamping it, so one unreadable poll cannot manufacture a
        match.
        """
        key = (row.id, row.instance_token or "")
        changed_at, previous = self._screen_digests.get(key, (0.0, ""))
        current = await self._screen_digest(provider, row)
        if current is None:
            return None  # No reading: unknown, and the previous one stands.
        if current != previous:
            self._screen_digests[key] = (now, current)
            return 0.0 if previous else None  # First reading: nothing to compare.
        self._prune_progress_cache(now)
        return None if not previous else now - changed_at

    async def _deferral_verdict(
        self,
        deferral: NudgeDeferred | None,
        row: SessionRecord,
        provider,
        *,
        idle_seconds: float,
        lease_ttl: float,
        now: float,
    ) -> StalledDeferral:
        """What a refused nudge proves, and therefore what the ladder may do.

        The refusal has to *assert* that no draft is being typed
        (:meth:`NudgeDeferred.escalates_ladder`); that alone is not enough,
        because it is a statement about a screen and not about the agent.
        So the answer is one of three, in this order:

        :attr:`StalledDeferral.HOLD`
            Someone is demonstrably at the composer, or the harness's own
            record shows the conversation moving.  Nothing is spent and
            nothing is announced, exactly as before.
        :attr:`StalledDeferral.ESCALATE`
            The record is there and says the conversation stopped before the
            lease expired -- proof, from outside the terminal.  The rung is
            spent and the ladder climbs as it always does.
        :attr:`StalledDeferral.REPORT`
            The harness keeps no record AQ can read (``opencode`` has no
            reader and no session identity to scope one to), or the one it
            names could not be stat'ed.  That is *unknown*, and unknown is
            not evidence: the stall is announced -- a WARNING and a
            ``task.stalled`` with ``evidence="unverified"``, quoting how long
            the pane has shown the same thing -- but no rung is spent and no
            claim is released on a signal AQ could not measure.

            Nothing about the terminal can upgrade that answer.  A pane that
            has stopped moving is what an agent between two writes looks like
            as much as what a wedged TUI looks like, so the screen is
            reported (:meth:`_screen_unchanged_seconds`) and never acted on.
            A CLI that keeps its own store is the exception, and it is asked
            there rather than here: ``harness_progress`` reads OpenCode's store
            scoped to the session (:mod:`src.sessions.opencode_store`), so an
            ``opencode`` holder with a refused nudge now escalates on its own
            record instead of being reported as unmeasurable.

        The distinction is the whole fix.  Reading "cannot inspect" as
        "stalled" is how a wedged-but-live holder could be destroyed on the
        strength of an absence nobody measured; reading it as "safe" is how
        ``vivid-quest-44.3`` sat on its task for hours in silence.
        """
        if deferral is None or not deferral.escalates_ladder():
            return StalledDeferral.HOLD
        if lease_ttl > 0 and idle_seconds <= lease_ttl:
            return StalledDeferral.HOLD
        source, progress = await harness_progress(
            row, base_dir=self.transcript_base_dir, liveness=self._liveness_store
        )
        if progress is not None:
            if lease_ttl > 0 and now - progress <= lease_ttl:
                return StalledDeferral.HOLD  # It is still writing: alive.
            logger.warning(
                "Session %s (%s) on task %s is idle %.0fs with a %s composer and no "
                "%s progress since %s — spending a stall rung",
                row.id, row.name, row.task_id, idle_seconds, deferral.reason, source,
                time.strftime("%H:%M:%S", time.localtime(progress)),
            )
            return StalledDeferral.ESCALATE
        return StalledDeferral.REPORT

    async def _store_activity(
        self, row: SessionRecord, *, now: float
    ) -> OpenCodeActivity | None:
        """The harness's own record of what this session has written, cached.

        ``None`` for a harness whose CLI keeps no store AQ reads, and ``None``
        for a store that could not be read -- in both cases unknown, which the
        caller must hold on rather than treat as "nothing happened".

        Cached per holder instance for :data:`_WEDGE_SCAN_SECONDS`: this runs
        for every session whose pane still looks busy, and the store is tens of
        gigabytes.  A failed read is cached like a successful one so an
        unreadable store cannot turn into a read on every tick either.
        """
        store = self._liveness_store(row.harness)
        if store is None:
            return None
        key = (row.id, row.instance_token or "")
        cached = self._wedge_reads.get(key)
        if cached is not None and now - cached[0] < _WEDGE_SCAN_SECONDS:
            return cached[1]
        activity = await asyncio.to_thread(store.activity, row.work_dir, store_lower_bound(row))
        self._prune_progress_cache(now)
        self._wedge_reads[key] = (now, activity)
        return activity

    async def _wedged_mid_turn(
        self, row: SessionRecord, *, now: float, lease_ttl: float
    ) -> OpenCodeActivity | None:
        """A session that reports itself busy while provably doing nothing.

        The gap this closes is not the ladder's but its *gate*.  Everything
        below the ``now - last_activity > lease_ttl`` check assumed the pane's
        own clock meant the agent was alive; for a TUI that repaints its
        in-turn spinner it does not.  ``crisp-horizon-90.10`` (2026-10-03) sat
        like that for 42 minutes -- a byte-identical pane, an Ollama holding no
        model, and zero stall events because the gate never opened.

        So a stall is declared here from four independent readings, and
        **all** of them must hold:

        * the harness's own store has written nothing for this session for
          longer than the lease (:meth:`_store_activity`) -- not a pane-derived
          guess, and not an absence: the session must have written rows, or
          there is nothing that could have stopped;
        * that store says no tool call is still open, so the silence is not one
          long build or one long ``bash``;
        * the provider reports positively that nothing is resident or in flight
          (:func:`~src.sessions.provider_liveness.request_inflight`), which is
          the difference between "not writing" and "not generating";
        * and the pane must have claimed otherwise -- the caller only asks once
          the ordinary idle check has passed, so a genuinely quiet session
          reaches this by its own path and not as a special case.

        Anything unknown -- no store for the harness, an unreadable one, an
        endpoint AQ cannot name or cannot reach -- is not a wedge.  The rest of
        the ladder then applies unchanged, including the composer guard: a
        person at the keyboard still holds it.
        """
        activity = await self._store_activity(row, now=now)
        if activity is None or not activity.has_rows:
            return None
        if now - activity.progress_at <= lease_ttl:
            return None  # Its own record says the turn is moving.
        if activity.inflight:
            return None  # A tool call is still running: silence is the work.
        if await request_inflight(row, self.config) is not False:
            return None  # A generation may be running, or nobody could say.
        logger.warning(
            "Session %s (%s) on task %s has been reporting activity for %.0fs while its "
            "own record has not moved since %s and its provider holds no model — "
            "declaring the turn stalled from its own record",
            row.id, row.name, row.task_id, now - (row.last_activity or row.started_at or now),
            time.strftime("%H:%M:%S", time.localtime(activity.progress_at)),
        )
        return activity

    async def _announce_unverified_stall(
        self, row: SessionRecord, task, deferral: NudgeDeferred, *, idle_seconds: float, now: float
    ) -> None:
        """Say that a holder is stuck behind a composer AQ cannot read.

        The event is the fix's teeth where proof is missing: this is the stall
        that produced *zero* ``task.stalled`` events for four hours on
        2026-10-03, so an operator and the digest had nothing to notice.  It
        carries ``evidence="unverified"`` so nothing downstream mistakes it for
        a rung, and how long the pane has shown the same thing, so the person
        reading it can tell a wedged TUI from an agent thinking — the one
        judgement AQ deliberately refuses to make for them.

        It repeats at most every :data:`_STALL_REPORT_INTERVAL_SECONDS` per
        holder instance: not per tick, and not once and then never again,
        because a stall that will never escalate is precisely the case that
        must not go quiet.
        """
        key = (row.id, row.instance_token or "")
        last = self._stall_reports.get(key)
        if last is not None and now - last < _STALL_REPORT_INTERVAL_SECONDS:
            return
        provider = self._provider_for(row)
        frozen_for = (
            None if provider is None
            else await self._screen_unchanged_seconds(provider, row, now=now)
        )
        self._stall_reports[key] = now
        self._prune_progress_cache(now)
        logger.warning(
            "Session %s (%s) on task %s is idle %.0fs and its %s composer cannot be "
            "read or cleared%s; no stall rung is spent because this harness has no "
            "progress record to corroborate it — reported instead",
            row.id, row.name, row.task_id, idle_seconds, deferral.reason,
            "" if frozen_for is None
            else f" and its pane has shown the same thing for {int(frozen_for)}s",
        )
        await self._emit(
            "task.stalled",
            task_id=row.task_id,
            project_id=row.project_id,
            title=task.title,
            session_id=row.id,
            idle_seconds=idle_seconds,
            deferred_reason=str(deferral.reason),
            evidence="unverified",
            **({} if frozen_for is None else {"screen_unchanged_seconds": int(frozen_for)}),
        )

    def _prune_progress_cache(self, now: float) -> None:
        """Drop readings older than the report interval, keeping both maps bounded."""
        for key, (changed_at, _) in list(self._screen_digests.items()):
            if now - changed_at > 4 * _STALL_REPORT_INTERVAL_SECONDS:
                del self._screen_digests[key]
        for key, seen_at in list(self._stall_reports.items()):
            if now - seen_at > 4 * _STALL_REPORT_INTERVAL_SECONDS:
                del self._stall_reports[key]
        for key, (read_at, _) in list(self._wedge_reads.items()):
            if now - read_at > 4 * _STALL_REPORT_INTERVAL_SECONDS:
                del self._wedge_reads[key]

    # -- step 4: stall ladder ---------------------------------------------

    async def _exit_usage_limit_screen(
        self, provider, row: SessionRecord, task, now: float,
        *, screen: UsageLimitScreen | None = None,
    ) -> bool:
        """Take a stalled session parked on its usage-limit screen out as a ``RATE_LIMIT`` exit.

        A CLI that hits its provider's usage limit mid-task usually does not
        exit — it prints the limit line and sits at its prompt — so the exit
        classifier never sees it and the provider-failover in-flight path
        (D13) never runs.  Left to the ladder it is nudged
        ``stall_max_nudges`` times into a CLI that cannot answer, then
        restarted ~23 minutes in with a restart spent and no provider
        evidence recorded.

        Instead, when the pane's tail is one of the CLIs' own blocking limit
        messages (:func:`~src.sessions.usage_limit_screen.match_usage_limit_screen`,
        deliberately far stricter than the exit classifier's patterns), the
        process is stopped and the session goes through :meth:`_apply_verdict`
        exactly as a death on a usage limit would: ``exit_rate_limit``
        evidence, then the failover exit path — checkpoint, hand-off, requeue
        — where it is wired, or the RATE_LIMIT pause / pool-key cooldown
        where it is not.  No restart is spent.

        ``provider_failover.mode: enforce`` only — ``observe`` and ``off`` keep
        the ladder as it was.  A stop that fails leaves everything to the
        ladder: nothing is released while the process may still be alive.
        Returns True when the session was handed off.
        """
        failover = getattr(self.config, "provider_failover", None)
        if failover is None or not failover.enforcing:
            return False
        screen = screen or await self._usage_limit_screen(provider, row)
        if screen is None:
            return False
        logger.warning(
            "Session %s (%s) on task %s is parked on a usage-limit screen (%r) after "
            "%.0fs without progress — stopping it and taking it out as a rate-limit exit",
            row.id,
            row.name,
            row.task_id,
            screen.line,
            now - (row.last_activity or row.started_at),
        )
        from src.providers.inflight import HANDOFF_SCREEN_LINES

        # Read before the stop takes the pane with it: the next worker's
        # hand-off note quotes what this one was doing.
        last_screen = await self._peek(provider, row, HANDOFF_SCREEN_LINES)
        try:
            await provider.stop(self._handle(row), grace=2.0)
        except Exception:
            logger.warning(
                "Stopping usage-limited session %s failed — leaving it to the stall ladder",
                row.id,
                exc_info=True,
            )
            return False
        # Whatever comes next, this session's ladder is over; the next
        # session on the task starts a fresh one (as a stall restart does).
        await self.db.set_task_meta(row.task_id, META_STALL_NUDGES, "0")
        await self.db.set_task_meta(row.task_id, META_STALL_LAST_ACTION, str(now))
        verdict = ExitVerdict(
            Verdict.RATE_LIMIT,
            f"usage-limit screen on a stalled session: {screen.line[:160]}",
            cooldown_seconds=screen.retry_after or DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
            usage_exhausted=screen.usage_exhausted,
            resets_at=now + screen.retry_after if screen.retry_after else None,
        )
        await self._apply_verdict(provider, row, task, verdict, now, screen=last_screen)
        return True

    async def _step_stall_ladder(self, live: list[SessionRecord], now: float) -> None:
        """Nudge → backoff → restart → quarantine.

        A stalled agent is not a dead agent.  Killing on timeout throws away
        the work in progress; nudging asks it to report or finish first.

        The one stall that nudging cannot help is a CLI parked on its
        provider's usage-limit screen: before every rung the ladder checks
        for that (:meth:`_exit_usage_limit_screen`) and, when it finds it,
        hands the session to the exit path instead of climbing.

        The gate this ladder starts from is the pane's own clock, which a TUI
        can keep fresh while doing nothing at all; :meth:`_wedged_mid_turn`
        overrides it from the harness's record and the provider's, and then
        every rung below behaves exactly as it does for any other stall.
        """
        ttl = float(self.sessions_config.lease_ttl_seconds)
        if ttl <= 0:
            return
        for row in live:
            if row.lifecycle not in ("task", "pool") or not row.task_id or row.state != "running":
                continue
            if row.lifecycle == "pool" and row.claim_phase != "active":
                continue
            if await self._waiting_for_question(row, now):
                continue
            waiting, resumed = await self._wait_lease(row, now)
            if waiting:
                continue
            # OpenCode's retry countdown continuously repaints its pane. A
            # blocking retry footer is positive evidence even when that
            # activity clock is fresh and the remote backend has no probe.
            failover = getattr(self.config, "provider_failover", None)
            checked_limit = False
            if self._runs_opencode(row) and failover is not None and failover.enforcing:
                provider = self._provider_for(row)
                if provider is not None:
                    screen = await self._usage_limit_screen(provider, row)
                    checked_limit = True
                    fresh = await self._still_live(row) if screen is not None else None
                    if (
                        fresh is not None
                        and fresh.state == "running"
                        and fresh.instance_token == row.instance_token
                        and fresh.task_id == row.task_id
                        and fresh.claim_phase == row.claim_phase
                    ):
                        task = await self.db.get_task(row.task_id)
                        if task is not None and task.status is TaskStatus.IN_PROGRESS:
                            if await self._exit_usage_limit_screen(
                                provider, row, task, now, screen=screen
                            ):
                                continue
            last = max(row.last_activity or row.started_at or 0.0, resumed)
            wedged: OpenCodeActivity | None = None
            if now - last <= ttl:
                # A pane that keeps painting is not evidence that anything is
                # working: an OpenCode TUI repaints its in-turn spinner forever,
                # so this gate never opened for crisp-horizon-90.10 and the
                # holder was never even announced.  Ask the session's own
                # record and its provider before believing the pane.
                wedged = await self._wedged_mid_turn(row, now=now, lease_ttl=ttl)
                if wedged is None:
                    continue
                last = min(last, wedged.progress_at)
            if await self._still_live(row) is None:
                continue  # already stopped/slept/quarantined this tick

            provider = self._provider_for(row)
            if provider is None:
                continue

            rungs = int(await self.db.get_task_meta(row.task_id, META_STALL_NUDGES) or 0)
            last_action = float(
                await self.db.get_task_meta(row.task_id, META_STALL_LAST_ACTION) or 0.0
            )
            if last_action and now - last_action < self.sessions_config.stall_backoff_seconds:
                continue  # still inside this rung's backoff

            task = await self.db.get_task(row.task_id)
            if task is None or task.status is not TaskStatus.IN_PROGRESS:
                continue

            if not checked_limit and await self._exit_usage_limit_screen(provider, row, task, now):
                continue

            # A provider with no input channel (subprocess) has nothing to
            # nudge *with*, so the ladder skips its nudge rungs entirely
            # rather than burning three cycles talking to no one.
            can_nudge = provider.supports(Cap.NUDGE)

            nudge_due = can_nudge and rungs < self.sessions_config.stall_max_nudges
            delivered: bool | None = False
            deferral: NudgeDeferred | None = None
            if nudge_due:
                minutes = int((now - last) // 60)
                # A harness sitting at its idle prompt with an open claim
                # looks exactly like a stalled one from out here, and it is
                # the common case: the turn ended without ``aq task close``.
                # So the nudge asks for the close explicitly — "close or
                # continue" — rather than only "report status".  This is the
                # rung that runs *before* any exit handling, which is the
                # point: an idle prompt should be talked to, not reaped.
                outcome = await self._try_nudge(
                    provider, row, stall_reminder(row.task_id, minutes)
                )
                delivered, deferral = outcome.delivered, outcome.deferral
                if delivered is None:
                    verdict = await self._deferral_verdict(
                        deferral, row, provider,
                        idle_seconds=now - last, lease_ttl=ttl, now=now,
                    )
                    if verdict is StalledDeferral.HOLD:
                        # The composer belongs to a person, or the agent is
                        # demonstrably working. Waiting for an empty composer
                        # is not a failed attempt, and no person is ever
                        # escalated on.
                        continue
                    if verdict is StalledDeferral.REPORT:
                        # Provably not a draft, and AQ cannot measure whether
                        # the holder is working: announce it and change
                        # nothing. A rung here would be a claim released on a
                        # signal nobody took.
                        await self._announce_unverified_stall(
                            row, task, deferral, idle_seconds=now - last, now=now
                        )
                        continue

            # Only announce a stall once an action can actually be attempted;
            # a draft can defer many polls without generating repeated notices.
            if rungs == 0:
                await self._emit(
                    "task.stalled",
                    task_id=row.task_id,
                    project_id=row.project_id,
                    title=task.title,
                    session_id=row.id,
                    idle_seconds=now - last,
                    **({"evidence": "store_stalled"} if wedged is not None else {}),
                    **({"deferred_reason": str(deferral.reason)} if deferral else {}),
                )

            if nudge_due:
                await self.db.set_task_meta(row.task_id, META_STALL_NUDGES, str(rungs + 1))
                await self.db.set_task_meta(row.task_id, META_STALL_LAST_ACTION, str(now))
                if delivered:
                    await self._emit(
                        "task.nudged",
                        task_id=row.task_id,
                        project_id=row.project_id,
                        title=task.title,
                        session_id=row.id,
                        attempt=rungs + 1,
                    )
                continue

            # Rungs exhausted: interrupt, kill, and let the scheduler
            # relaunch with the harness resume key so context survives.
            if row.lifecycle == "pool":
                # No in-place restart for a pool session -- the pool step
                # starts a fresh one next tick; this one's claim (and the
                # task it holds) goes back through the normal termination
                # path.
                if self.orchestrator is None:
                    logger.warning(
                        "Pool session %s stalled but no orchestrator is wired "
                        "— skipping", row.id,
                    )
                    continue
                try:
                    await provider.interrupt(self._handle(row))
                except Exception:
                    logger.debug("interrupt failed for %s", row.id, exc_info=True)
                await self.orchestrator._terminate_pool_session(row, reason="stalled")
                continue
            count = await self.db.bump_session_restarts(row.id)
            # ``>=``, matching the rapid-crash branch.  ``>`` here allowed
            # exactly one restart more than ``max_restarts`` before
            # quarantine, so the two ladders disagreed about what the
            # budget meant.
            if count >= self.sessions_config.max_restarts:
                await self._quarantine(row, task, reason="stall", now=now)
                continue
            try:
                await provider.interrupt(self._handle(row))
            except Exception:
                logger.debug("interrupt failed for %s", row.id, exc_info=True)
            await self._stop_session(provider, row, reason="stall_restart")
            await self.db.set_task_meta(row.task_id, META_STALL_NUDGES, "0")
            await self.db.set_task_meta(row.task_id, META_STALL_LAST_ACTION, str(now))
            await self.db.transition_task(
                row.task_id,
                TaskStatus.PAUSED,
                context="session_stalled_restart",
                resume_after=now + self.sessions_config.restart_backoff_seconds,
                assigned_agent_id=None,
            )
            await self._carry_resume_key(row, task)
            await self._release_task(task, row, reason="stall")
            await self._emit(
                "task.restarted",
                task_id=row.task_id,
                project_id=row.project_id,
                title=task.title,
                session_id=row.id,
                attempt=count,
                reason="stall",
            )

    # -- step 4: orphans (row and task disagree) ---------------------------

    async def _step_orphans(self, live: list[SessionRecord], now: float) -> None:
        """Reconcile the two ways a session row and its task can disagree.

        Design §4.1's table has *"task already closed, session lingering →
        normal drain path (kill, ``stopped``)"*, but nothing implemented it:
        ``Verdict.DRAINED`` is only reachable from inside ``_step_exits``,
        which requires a **dead** process.  There are three ways in with the
        process still alive:

        * ``complete_session_task`` releases the workspace and IDLEs the
          agent at close time, before the ack — so an agent that closes and
          never acks leaves a reassignable worktree with a live agent in it;
        * ``stop_task`` finds no ``_adapters`` entry (the session fork never
          registers one) and no live ``_running_tasks`` entry, so it cancels
          nothing and leaves the session running;
        * ``aq session kill`` before its own fix.

        The mirror case — an open task whose session row is *not* live —
        is handled here too: nothing else would ever free it, because
        ``_step_exits`` and the ladder both iterate live rows only.

        Ordering matters for (a): ``_step_drain_ack`` runs earlier in the
        same tick, so an ack that has landed always wins and the agent gets
        the graceful path.  A terminal task is also a normal, short-lived
        state while a pool close moves from ``complete_session_task`` to
        ``release_claim``.  That interleaving must release only the task
        hold: pool sizing owns any later drain decision and its grace period.
        """
        # (a) live session, task closed or gone.
        for row in live:
            if row.lifecycle not in ("task", "pool"):
                continue
            if row.lifecycle == "pool" and row.task_id is None:
                continue  # idle pool session -- nothing to orphan-check
            if self._is_deferred(row.name):
                continue
            fresh = await self._still_live(row)
            if fresh is None:
                continue  # an earlier step in this tick already handled it
            row = fresh
            waiting, _ = await self._wait_lease(row, now)
            if waiting:
                continue
            task = await self.db.get_task(row.task_id) if row.task_id else None
            still_open = task is not None and is_live_pool_claim_task_status(task.status)
            if still_open:
                continue
            if row.lifecycle == "pool":
                # A terminal close can commit before its CLI response is
                # lost to a daemon restart.  Its attached integration owner
                # is intentionally a release guard, so ordinary reclaim
                # cannot repair it.  Ask the orchestrator to prove this
                # exact writer is gone and its terminal branch is clean and
                # published; a live, dirty, stale, or reused holder remains
                # untouched and the normal guard below keeps it fenced.
                if task is not None and task.status is TaskStatus.COMPLETED:
                    recover = getattr(
                        self.orchestrator, "arecover_completed_integration_pool_claim", None
                    )
                    if recover is not None:
                        try:
                            if await recover(task, row):
                                continue
                        except Exception:
                            logger.warning(
                                "Could not recover completed pool claim %s", row.id,
                                exc_info=True,
                            )
                # ``_cmd_task_close`` makes the task terminal before its
                # subsequent ``release_claim`` clears ``sessions.task_id``.
                # Releasing here is idempotent with that later close-path
                # release, while terminating would incorrectly bypass pool
                # scale-down grace and an explicit drain acknowledgement.
                claim_file = read_claim_file(row.work_dir) if row.work_dir else None
                cleanup_epoch = (
                    claim_file.get("claim_epoch")
                    if claim_file is not None and claim_file.get("task_id") == row.task_id
                    else task.claim_epoch if task is not None else row.last_claim_epoch
                )
                release = await self.db.release_claim(
                    row.id,
                    task_status=task.status if task is not None else TaskStatus.READY,
                    context=(
                        f"pool_claim_reclaimed:{task.status.value}"
                        if task is not None
                        else "pool_claim_reclaimed:missing"
                    ),
                    now=now,
                    expected_task_id=row.task_id,
                    expected_claim_epoch=row.last_claim_epoch,
                    expected_task_status=task.status if task is not None else None,
                    drain_after_release=self.config.swarm.fresh_context_per_task,
                    # The pool process still owns this checkout until termination.
                )
                if release.released and row.work_dir:
                    remove_claim_file_if_matches(
                        row.work_dir,
                        row.task_id,
                        cleanup_epoch,
                    )
                if release.released:
                    status = task.status.value if task is not None else "missing"
                    await self.db.create_message(
                        project_id=row.project_id,
                        from_kind="system",
                        from_id="session-reconciler",
                        to_kind="session",
                        to_id=row.id,
                        subject="Pool claim reclaimed",
                        body=(
                            f"Your claim on task {row.task_id} was reclaimed because its "
                            f"status is {status}. Run `aq task claim --next` for more work."
                        ),
                    )
                continue
            provider = self._provider_for(row)
            if provider is None:
                continue
            logger.info(
                "Session %s is live but task %s is %s — draining",
                row.id,
                row.task_id,
                getattr(getattr(task, "status", None), "value", "gone"),
            )
            await self._stop_session(provider, row, reason="task_closed")
            await self._emit(
                "session.exited",
                session_id=row.id,
                name=row.name,
                task_id=row.task_id,
                project_id=row.project_id,
                verdict=str(Verdict.DRAINED),
                reason="task_closed",
            )

        # (b) open task, no live row.  Nothing else looks at this: every
        # other step iterates ``live``, so a task whose session row went
        # non-live without a verdict would hold its agent and workspace
        # until the daemon restarted.
        live_task_ids = {r.task_id for r in live if r.lifecycle == "task" and r.task_id}
        try:
            stranded = await self.db.list_tasks(status=TaskStatus.IN_PROGRESS)
        except Exception:
            logger.debug("orphan sweep: cannot list in-progress tasks", exc_info=True)
            return
        launching = getattr(self.orchestrator, "_running_tasks", None) or {}
        for task in stranded:
            if task.id in live_task_ids:
                continue
            if task.id in launching:
                # ``_execute_task`` is still running for this task.  It goes
                # IN_PROGRESS *before* workspace preparation, which can be a
                # git clone taking minutes, and the session row is written
                # only after ``provider.start`` succeeds.  A retry therefore
                # spends that whole window as "IN_PROGRESS with a stopped
                # row from the previous attempt" — blocking it here would
                # kill every relaunch.
                continue
            row = await self.db.get_session_for_task(task.id)
            if row is None:
                # Never launched as a session (legacy runtime, or the
                # scheduler is mid-launch).  Not ours to touch.
                continue
            waiting, _ = await self._wait_lease(row, now)
            if waiting:
                continue
            if row.state in _LIVE_STATES:
                continue
            if self._is_deferred(row.name):
                continue
            logger.warning(
                "Task %s is IN_PROGRESS but session %s is %s — releasing",
                task.id,
                row.id,
                row.state,
            )
            await self.db.set_task_meta(task.id, "needs_attention", "session_not_live")
            await self.db.transition_task(
                task.id,
                TaskStatus.BLOCKED,
                context="session_not_live",
                assigned_agent_id=None,
            )
            await self._release_task(task, row, reason="session_not_live")
            await self._emit(
                "task.needs_attention",
                task_id=task.id,
                project_id=task.project_id,
                title=task.title,
                reason="session_not_live",
            )
            # This is terminal for the task, not merely an inconsistent
            # session row.  The attention event explains the operational
            # condition, while task.failed drives reflection and failure
            # notifications for the task that could no longer run.
            await self._emit(
                "task.failed",
                task_id=task.id,
                project_id=task.project_id,
                title=task.title,
                status=TaskStatus.BLOCKED.value,
                context="session_not_live",
                error=f"session {row.id} is {row.state}; task has no live session",
            )

    # -- idle stop intent --------------------------------------------------

    def _idle_stop_grace(self) -> float:
        """Seconds a finished session may keep its process before it is stopped."""
        return max(0.0, float(getattr(self.sessions_config, "idle_stop_grace_seconds", 0) or 0))

    async def _step_idle_stop_intent(
        self, live: list[SessionRecord], now: float
    ) -> None:
        """Stop a finished session's process once a bounded grace expires.

        The completion protocol asks an agent to *leave*: ``aq task close``
        releases the claim with ``drain_after_release``, and a claim over a
        spent budget answers ``session_exhausted``.  Both leave the durable
        intent that the session is done -- ``desired_state='stopped'``, or a
        claim budget nothing can extend -- and both say so in the worker
        prompt's exit instruction.  A harness that honours it exits and
        :meth:`_step_exits` classifies the death as it always did.

        An OpenCode worker does not.  Its turn ends, the pane stays, and the
        session keeps its pool slot, its agent and its worktree until someone
        runs ``aq session kill`` by hand: on 2026-10-03 two verifiers sat in
        exactly this state for 18 and 26 minutes after a passing and a failing
        close.  The daemon does not drive agents, but it may enforce a stop
        somebody already asked for.

        **Why the clock is the observation, not the pane.**  tmux's
        ``window_activity`` advances on *any* output, and an OpenCode TUI at
        its final summary keeps painting, so ``sessions.last_activity`` --
        which every other idle bound here reads -- never goes stale.  (That is
        also why ``_step_abandoned_pool_claim_loop`` never saw these workers:
        they counted as idle *supply*.)  So the grace runs from the first tick
        this step *observed* the finished shape, keyed by the instance token
        so a relaunched session starts its own clock.  A daemon restart costs
        at most one more grace, and ``sessions.stop_intent_pending`` reports
        anything that outlives several of them.

        Every exemption the other idle steps honour is honoured here too: an
        unlisted provider prefix, a task this daemon is still writing under
        its control lock, and a pending agent question, which a
        stopped-intent session can still be blocked on.  A durable wait needs
        no separate gate: one can only be registered against a session still
        holding its task ``running``, so the open-task shape below already
        covers it.

        The stop is the one ``aq session kill`` performs -- ``provider.stop``
        behind the instance-token fence -- and the pool teardown is
        ``_terminate_pool_session``, so the slot, the claim and the worktree
        come back the same way an operator's kill returns them.
        """
        grace = self._idle_stop_grace()
        if grace <= 0:
            return
        seen_at = self._idle_stop_intent_seen
        live_keys = set()
        for row in live:
            key = (row.id, row.instance_token)
            live_keys.add(key)
            if not await self._idle_stop_candidate(row, now=now):
                seen_at.pop(key, None)
                continue
            first = seen_at.setdefault(key, now)
            if now - first < grace:
                continue
            seen_at.pop(key, None)
            fresh = await self._still_live(row)
            if fresh is None or (fresh.id, fresh.instance_token) != key:
                continue
            if not await self._idle_stop_candidate(fresh, now=now):
                continue
            await self._stop_finished_session(fresh, now=now)
        # A relaunched session gets a new instance token, and a stopped row
        # leaves ``live``: both must not leave the map growing forever.
        for key in [k for k in seen_at if k not in live_keys]:
            seen_at.pop(key, None)

    async def _idle_stop_candidate(self, row: SessionRecord, *, now: float) -> bool:
        """Whether *row* has nothing left to do at all.

        The shape is the safety proof, and it never consults a clock a
        painting pane can keep fresh.  A session halfway through a claim
        (``claiming`` / ``preparing``) still has work -- the claim itself --
        so it is exempt here.  A session that *holds* an active claim is
        judged by the gates that follow: an open task or a control lock it is
        still written under keeps it, while a spent task plus a recorded stop
        intent (the ``retain_claim`` shape, where the close is terminal but
        the writer is deliberately kept over an unproven branch handoff) is
        exactly the one this path is for -- the worker left its work and only
        the process remains.  Ordered cheapest gate first, so the one lookup
        a *running* worker cannot avoid (``get_profile``, for the claim
        budget) is only reached by a worker that holds no open task.
        """
        if row.claim_phase in ("claiming", "preparing"):
            return False
        if self._is_deferred(row.name):
            return False
        if row.task_id:
            task = await self.db.get_task(row.task_id)
            if task is not None and is_live_pool_claim_task_status(task.status):
                return False
            # A close that has committed but not finished its tail (branch
            # handoff under the task's control lock) still owns this process.
            if self._task_control_held(row.task_id):
                return False
        if row.desired_state == "stopped":
            finished = True
        elif row.lifecycle == "pool" and row.claims:
            # ``session_exhausted``: the budget ``take_claim_slot`` enforces
            # is spent, so the worker protocol's answer is to leave.
            profile = await self._profile_for(row)
            finished = profile is not None and pool_claim_budget_exhausted(
                row, pool_claim_cap(self.config, profile)
            )
        else:
            finished = False
        if not finished:
            return False
        return not await self._waiting_for_question(row, now)

    def _task_control_held(self, task_id: str) -> bool:
        """Whether this daemon is still writing *task_id* under its control lock."""
        held = getattr(self.orchestrator, "_task_control_held", None)
        if held is None:
            return False
        try:
            return bool(held(task_id))
        except Exception:
            logger.debug("task control probe failed for %s", task_id, exc_info=True)
            return True

    async def _stop_finished_session(self, row: SessionRecord, *, now: float) -> None:
        """The fenced stop ``aq session kill`` performs, then the slot release."""
        logger.warning(
            "Session %s (%s) holds a finished %s task and has been idle with a recorded "
            "stop intent; stopping its process",
            row.id,
            row.name,
            row.lifecycle,
        )
        if row.lifecycle == "pool":
            if self.orchestrator is None:
                logger.warning(
                    "Pool session %s has a stop intent but no orchestrator is wired "
                    "— skipping", row.id,
                )
                return
            await self.orchestrator._terminate_pool_session(row, reason="stop_intent_idle")
            await self._emit(
                "session.stop_intent_stopped",
                session_id=row.id,
                name=row.name,
                task_id=row.task_id,
                project_id=row.project_id,
                lifecycle=row.lifecycle,
                idle_seconds=int(now - (row.last_activity or row.started_at or now)),
            )
            return
        provider = self._provider_for(row)
        if provider is None:
            return
        try:
            await provider.stop(self._handle(row), grace=2.0)
        except Exception:
            logger.warning("Stopping session %s failed", row.id, exc_info=True)
            return
        # ``state`` is deliberately left alone: ``_step_exits`` iterates live
        # rows and classifies the death, releasing the task and the agent.  A
        # database write here would be a claim about a process only a fresh
        # probe can support.
        await self._emit(
            "session.stop_intent_stopped",
            session_id=row.id,
            name=row.name,
            task_id=row.task_id,
            project_id=row.project_id,
            lifecycle=row.lifecycle,
            idle_seconds=int(now - (row.last_activity or row.started_at or now)),
        )

    # -- step 5: named desired-state ---------------------------------------

    async def _step_named(self, live: list[SessionRecord], now: float) -> None:
        """Converge persistent sessions toward their declared intent.

        Both directions, since ``sessions.desired_state`` exists to say
        which one is wanted (see
        ``docs/superpowers/specs/2026-08-27-session-desired-state-design.md``):

        * **down** — a running session past its profile's ``idle_timeout``
          is drained to ``sleeping``, and its *intent* becomes ``sleeping``
          at the same moment.  That second half is what stops the flap: a
          drained session stops being wanted, so the up-branch does not
          immediately undo the down-branch.
        * **up** — a non-live row still marked ``desired_state="running"``
          is started.  Waking is always an explicit act (an inbound message
          via the lens, or ``aq session wake``), never an inference.

        Starting is delegated to :attr:`starter`, not reimplemented here:
        the lens owns token minting, the global-supervisor special cases
        and work_dir resolution, and two copies of that would drift.
        """
        await self._converge_named_up(now)
        idle_rows = [r for r in live if r.lifecycle == "named" and r.state == "running"]
        if not idle_rows:
            return
        for row in idle_rows:
            # Named supervisors have no task stall ladder. Recover an owned
            # notification even if its message was consumed through the inbox
            # and therefore no longer produces another delivery attempt.
            provider = self._provider_for(row)
            resubmit = getattr(provider, "resubmit_pending", None)
            if callable(resubmit):
                try:
                    if await resubmit(self._handle(row)):
                        # Submission starts fresh work even if the previous
                        # activity sample had already crossed the idle limit.
                        await self.db.touch_session_activity(row.id, now)
                        continue
                except Exception:
                    logger.debug("pending supervisor submit remains recoverable", exc_info=True)
            profile = await self._profile_for(row)
            idle_timeout = int(getattr(profile, "idle_timeout", 0) or 0)
            if idle_timeout <= 0:
                continue
            last = row.last_activity or row.started_at
            if now - last <= idle_timeout:
                continue
            if row.profile_id == "supervisor" or self._named_address(row) is not None:
                try:
                    if await self.db.has_supervision_work(project_id=row.project_id):
                        continue
                except Exception:
                    # Uncertain fleet state is not evidence that supervision
                    # is finished. Keep the session and retry on the next tick.
                    logger.warning("deferring supervisor idle sleep for %s", row.name,
                                   exc_info=True)
                    continue
            provider = self._provider_for(row)
            if provider is None:
                continue
            await self._stop_session(
                provider, row, reason="idle_timeout", state="sleeping"
            )
            await self._emit(
                "session.sleeping",
                session_id=row.id,
                name=row.name,
                project_id=row.project_id,
                reason="idle_timeout",
            )

    async def _converge_named_up(self, now: float) -> None:
        """Start named rows that are wanted but not live.

        Never destructive, and never the *first* attempt at a start — the
        lens starts a supervisor synchronously when a message arrives.  This
        is the safety net for the case where that start failed, or the
        process died later without anything noticing: the intent survives in
        the row, so the next tick tries again.

        Failures spend the stall ladder's budget (``max_restarts``,
        ``restart_backoff_seconds``) rather than retrying every 5 s forever;
        a permanently misconfigured supervisor reaches ``quarantined`` and
        stops costing an attempt per tick.
        """
        if self.starter is None:
            logger.debug("no session starter wired; named up-convergence disabled")
            return
        try:
            wanted = await self.db.list_sessions(
                lifecycle="named", desired_state="running"
            )
        except Exception:
            logger.debug("listing wanted named sessions failed", exc_info=True)
            return
        for row in wanted:
            # Generic agent terminals are explicitly started by the operator.
            # They have no supervisor address and must not spend its retry
            # budget (or be quarantined without any launch being attempted).
            address = self._named_address(row)
            if address is None or row.state in _LIVE_STATES:
                continue
            if self._is_deferred(row.name):
                continue
            last = row.last_activity or row.started_at or 0.0
            backoff = self.sessions_config.restart_backoff_seconds * max(row.restarts, 1)
            if now - last < backoff:
                continue
            count = await self.db.bump_session_restarts(row.id)
            if count >= self.sessions_config.max_restarts:
                await self._quarantine(row, None, reason="start_failed", now=now)
                continue
            # ``last_activity`` doubles as the backoff clock for a row that
            # is not running: without stamping it, every tick would compute
            # the same elapsed time and retry immediately.
            await self.db.update_session(row.id, last_activity=now)
            try:
                started = await self.starter.ensure_started(
                    kind="session", target_id=address, project_id=row.project_id
                )
            except Exception:
                logger.warning("starting named session %s failed", row.name, exc_info=True)
                continue
            if not started:
                logger.debug("starter declined to start %s", row.name)
                continue
            # The intent has been satisfied -- by a *new* row, since the
            # lens inserts one per cold start.  Retire this row's intent so
            # the next tick does not start a second session for the same
            # want.  Guarded on the row still being non-live: if the start
            # was a no-op because the process was alive all along, the row
            # is the live one and its intent must stand.
            fresh = await self.db.get_session(row.id)
            if fresh is not None and fresh.state not in _LIVE_STATES:
                await self.db.update_session(row.id, desired_state="stopped")
            await self._emit(
                "session.started",
                session_id=row.id,
                name=row.name,
                project_id=row.project_id,
                reason="desired_running",
            )

    @staticmethod
    def _named_address(row: SessionRecord) -> str | None:
        """Runtime session name -> messaging address.

        The inverse of the lens's ``_resolve_runtime_session_name``:
        ``n-supervisor--<pid>`` addresses as ``supervisor-<pid>``.  Only
        supervisor-named sessions are wake-on-demand today; anything else
        returns None rather than guessing an address the lens would reject.
        """
        name = row.name or ""
        if not name.startswith("n-supervisor--"):
            return None
        return "supervisor-" + name[len("n-supervisor--") :]

    def _is_deferred(self, name: str) -> bool:
        """True when this tick could not enumerate *name*'s provider."""
        return any(name.startswith(prefix) for prefix in self._deferred_prefixes)

    async def _profile_for(self, row: SessionRecord):
        try:
            return await self.db.get_profile(row.profile_id)
        except Exception:  # noqa: BLE001 -- missing profile is handled as unknown
            return None

    # -- step 6: backstop --------------------------------------------------

    async def _step_backstop(self, live: list[SessionRecord], now: float) -> None:
        """The final net above the ladder — not the primary defense.

        ``agents.stuck_timeout_seconds`` used to be an ``asyncio.wait_for``
        that killed work outright.  Here it only fires after the ladder has
        had its full run, and it force-kills rather than silently dropping.

        An idle pool session (``task_id is None``) is never stale here — it
        is not holding anyone's work, so there is nothing this backstop is
        protecting against.  A pool session holding a task is keyed on
        *inactivity* (``last_activity``, falling back to ``started_at`` only
        when there is no activity signal yet), not on how long the session
        has existed — a healthy long-lived pool session must not be
        force-killed just for being old.
        """
        limit = float(getattr(self.config.agents_config, "stuck_timeout_seconds", 0) or 0)
        if limit <= 0:
            return
        for row in live:
            if row.lifecycle not in ("task", "pool") or not row.task_id:
                continue
            if await self._waiting_for_question(row, now):
                continue
            waiting, resumed = await self._wait_lease(row, now)
            if waiting:
                continue
            if resumed and now - resumed <= float(self.sessions_config.lease_ttl_seconds):
                continue  # one full lease interval to consume the result
            if row.lifecycle == "pool":
                last = row.last_activity if row.last_activity is not None else row.started_at
                last = max(last or 0.0, resumed)
                elapsed = now - (last or now)
            else:
                baseline = row.started_at
                questions = getattr(self.orchestrator, "agent_questions", None)
                if questions is not None:
                    resumed_at = await questions.backstop_activity_at(row)
                    if resumed_at is not None:
                        baseline = resumed_at
                baseline = max(baseline or 0.0, resumed)
                elapsed = now - (baseline or now)
            if elapsed <= limit:
                continue
            fresh = await self._still_live(row)
            if fresh is None:
                continue  # already stopped/slept/quarantined this tick
            row = fresh
            if row.lifecycle == "pool":
                if self.orchestrator is None:
                    logger.warning(
                        "Pool session %s exceeded stuck_timeout_seconds but no "
                        "orchestrator is wired — skipping", row.id,
                    )
                    continue
                task = await self.db.get_task(row.task_id)
                logger.warning(
                    "Pool session %s exceeded stuck_timeout_seconds (%ss) — terminating",
                    row.id,
                    limit,
                )
                if task is not None:
                    await self.db.set_task_meta(
                        task.id, "needs_attention", "exited_holding_task"
                    )
                await self.orchestrator._terminate_pool_session(row, reason="stuck_timeout")
                continue
            provider = self._provider_for(row)
            if provider is None:
                continue
            task = await self.db.get_task(row.task_id)
            logger.warning(
                "Session %s exceeded stuck_timeout_seconds (%ss) — force-killing",
                row.id,
                limit,
            )
            await self._stop_session(provider, row, reason="stuck_timeout")
            if task is not None and task.status is TaskStatus.IN_PROGRESS:
                await self.db.set_task_meta(task.id, "needs_attention", "stuck_timeout")
                await self.db.transition_task(
                    task.id,
                    TaskStatus.BLOCKED,
                    context="stuck_timeout",
                    assigned_agent_id=None,
                )
                await self._release_task(task, row, reason="stuck_timeout")
                await self._emit(
                    "task.quarantined",
                    task_id=task.id,
                    project_id=task.project_id,
                    title=task.title,
                    session_id=row.id,
                    reason="stuck_timeout",
                )
                await self._emit(
                    "task.failed",
                    task_id=task.id,
                    project_id=task.project_id,
                    title=task.title,
                    status=TaskStatus.BLOCKED.value,
                    context="stuck_timeout",
                    error=(
                        f"session {row.id} exceeded stuck_timeout_seconds "
                        f"({limit:g}s)"
                    ),
                )

    # -- shared actions ----------------------------------------------------

    async def _still_live(self, row: SessionRecord) -> SessionRecord | None:
        """Re-read *row*, returning it only if it is still in a live state.

        ``live`` is snapshotted once per tick by ``_step_observe``, but the
        steps that follow mutate it.  A later step acting on the snapshot
        would undo an earlier one — the exit classifier sleeps a session
        with ``sleep_reason="rate_limit"``, and then the orphan step, still
        holding the pre-tick row, sees a PAUSED task and stops it.  Every
        step that *writes* re-reads first.
        """
        try:
            fresh = await self.db.get_session(row.id)
        except Exception:
            logger.debug("could not re-read session %s", row.id, exc_info=True)
            return None
        if fresh is None or fresh.state not in _LIVE_STATES:
            return None
        return fresh

    async def _release_task(self, task, session: SessionRecord | None = None, *,
                             reason: str = "released") -> None:
        """Free the agent and the workspace lock a terminal task was holding.

        Every non-DRAINED verdict owes this.  The legacy runtime always did
        it (``execution.py``'s timeout / error / failure branches all call
        ``update_agent(..., IDLE)`` and ``_release_workspaces_for_task``);
        the first cut of this module transitioned the task and stopped,
        which is a regression, not parity.

        It matters because ``AgentReconciler``'s orphan sweep only resets a
        BUSY agent whose *task row is missing* -- a PAUSED or BLOCKED task
        still has one, so the agent stayed BUSY and the workspace stayed
        locked until the daemon restarted.  With N crash-looping tasks that
        is N agents and N workspaces burned, and the re-queued task cannot
        acquire a workspace because its own dead predecessor still holds
        the lock.

        A pool session is never the generic release path: it goes through
        ``_terminate_pool_session`` so its process is confirmed stopped before
        the durable worker becomes available for another session.
        """
        if task is None or self.orchestrator is None:
            return
        if session is not None and session.lifecycle == "pool":
            await self.orchestrator._terminate_pool_session(session, reason=reason)
            return
        release_integration = getattr(
            self.orchestrator, "arelease_integration_writer_for_retry", None
        )
        if release_integration is not None:
            released = await release_integration(task, reason=reason)
            if released is False:
                # Unknown termination/detach is not release evidence.  Keep
                # the workspace locked so no successor can write concurrently.
                return
        release = getattr(self.orchestrator, "release_session_task_resources", None)
        if release is None:
            return
        try:
            await release(task.id, agent_id=task.assigned_agent_id, expect_claim_epoch=task.claim_epoch)
        except Exception:
            logger.exception(
                "Session reconciler: releasing resources for task %s failed",
                task.id,
            )

    async def _carry_resume_key(self, row: SessionRecord, task) -> None:
        """Hand this session's conversation id to the task that outlives it.

        ``--session-id`` pinned the harness's own id to ours at launch, so
        ``sessions.session_key`` *is* the ``--resume`` argument.  Writing it
        into task metadata is the whole of "relaunch with ``--resume`` so
        conversation context survives" -- ``_launch_session_for_task``
        already reads ``session_resume_key`` back on the next start, and
        before this nothing ever wrote it.
        """
        if task is None or not row.session_key:
            return
        if row.harness == "codex" and row.session_key == row.id:
            return  # Legacy AQ UUID placeholders are not Codex resume identities.
        try:
            await self.db.set_task_meta(task.id, "session_resume_key", row.session_key)
        except Exception:
            logger.debug("could not persist resume key for task %s", task.id, exc_info=True)

    async def _try_nudge(self, provider, row: SessionRecord, text: str) -> _NudgeOutcome:
        """Deliver *text* to *row*, and say which of the three things happened."""
        if not provider.supports(Cap.NUDGE):
            return _NudgeOutcome(False)
        try:
            await provider.nudge(self._handle(row), text)
            return _NudgeOutcome(True)
        except NudgeDeferred as exc:
            logger.debug(
                "Nudge to session %s deferred (%s); terminal input untouched", row.id, exc
            )
            return _NudgeOutcome(None, deferral=exc)
        except NotSubmitted as exc:
            # WARNING, not info: text left in a composer blocks every later
            # nudge on the empty-composer guard, so "will retry" can mean
            # "never" — the operator has to be able to see it in the log.
            dirty = bool(getattr(exc, "composer_dirty", False))
            logger.warning(
                "Nudge to session %s (%s) on task %s pasted but not submitted: %s%s",
                row.id,
                row.name,
                row.task_id,
                exc,
                " — text is stuck in the composer" if dirty else " — will retry",
            )
            await self._emit(
                "session.nudge_unsubmitted",
                session_id=row.id,
                name=row.name,
                task_id=row.task_id,
                project_id=row.project_id,
                composer_dirty=dirty,
                reason=str(exc),
            )
            return _NudgeOutcome(False)
        except CapabilityUnsupported:
            return _NudgeOutcome(False)
        except Exception:
            logger.debug("nudge failed for %s", row.id, exc_info=True)
            return _NudgeOutcome(False)

    async def _stop_session(
        self,
        provider,
        row: SessionRecord,
        *,
        reason: str,
        state: str = "stopped",
    ) -> None:
        try:
            await provider.stop(self._handle(row), grace=2.0)
        except Exception:
            logger.warning("Stopping session %s failed", row.id, exc_info=True)
        # ``sleep_reason`` is forensics: why this session is not running.
        # Only *write* it when this call is the reason.  Re-sending the
        # stale in-memory value let a backstop kill on a RATE_LIMIT-slept
        # session overwrite ``"rate_limit"`` with ``None`` -- destroying the
        # one field that explained what happened.
        fields = {"state": state, "desired_state": state, "end_reason": reason}
        if state == "sleeping":
            fields["sleep_reason"] = reason
        await self.db.update_session(row.id, **fields)

    async def _quarantine(self, row: SessionRecord, task, *, reason: str, now: float) -> None:
        """Terminal by default — nothing auto-releases a quarantine."""
        provider = self._provider_for(row)
        if provider is not None:
            try:
                await provider.stop(self._handle(row), grace=2.0)
            except Exception:
                logger.debug("stop during quarantine failed for %s", row.id, exc_info=True)
        await self.db.update_session(
            row.id,
            # Stopped is terminal in the session state machine. Failed named
            # cold starts still need their restart intent retired and their
            # quarantine evidence recorded without an illegal state change.
            state="stopped" if row.state == "stopped" else "quarantined",
            desired_state="stopped",
            quarantined_at=now,
            ended_at=now,
            end_reason=reason,
            sleep_reason=reason,
        )
        await self._emit(
            "session.quarantined",
            session_id=row.id,
            name=row.name,
            task_id=row.task_id,
            project_id=row.project_id,
            reason=reason,
        )
        if row.lifecycle == "pool":
            await self._emit(
                "pool.session_quarantined",
                project_id=row.project_id,
                profile_id=row.profile_id,
                session_id=row.id,
                name=row.name,
                reason=reason,
            )
        if task is not None:
            await self.db.set_task_meta(task.id, "needs_attention", f"session_{reason}")
            await self.db.transition_task(
                task.id,
                TaskStatus.BLOCKED,
                context=f"session_{reason}",
                assigned_agent_id=None,
            )
            await self._release_task(task, row, reason=reason)
            await self._emit(
                "task.quarantined",
                task_id=task.id,
                project_id=task.project_id,
                title=task.title,
                session_id=row.id,
                reason=reason,
            )
            await self._emit(
                "task.failed",
                task_id=task.id,
                project_id=task.project_id,
                title=task.title,
                status=TaskStatus.BLOCKED.value,
                context=f"session_{reason}",
                error=f"session {row.id} quarantined: {reason}",
            )
