"""Reconcile desired session state against observed session state.

One ``tick()`` per orchestrator cycle (~5 s), zero LLM calls, deterministic.
The daemon does not *drive* agents — it observes them and converges.

``tick()`` runs six steps in a fixed order, each isolated so a failure in
one does not skip the rest:

1. **Refresh observation** — who is actually alive.
2. **Drain-ack** — the explicit end of the completion protocol.
3. **Exit classifier** — dead process, task still open → typed verdict.
4. **Orphans** — the two ways row and task can disagree: a live session
   whose task is no longer open (kill it), and an open task whose session
   row is not live (release it).
5. **Stall ladder** — alive but silent: nudge → restart → quarantine.
6. **Named desired-state** — converge persistent sessions (start/sleep).
7. **Backstop** — ``stuck_timeout_seconds`` as the final net, not the
   primary defense.

The single most important rule in this module: **unknown is not dead.**  A
``PartialListError`` from a provider, or a failed secondary probe, defers
every destructive action for that prefix.  The classifier acts on positive
evidence of death and on nothing else.
"""

from __future__ import annotations

import dataclasses
import logging
import time
import uuid
from dataclasses import dataclass, field

from src.models import SessionRecord, TaskStatus
from src.sessions.exit_classifier import ExitVerdict, Verdict, classify_exit
from src.sessions.provider import (
    Cap,
    CapabilityUnsupported,
    NotSubmitted,
    PartialListError,
    SessionHandle,
)

logger = logging.getLogger(__name__)

__all__ = ["SessionReconciler", "AdoptReport", "DRAIN_ACK_KEY"]

#: Provider-side metadata key the agent's ``aq session drain-ack`` sets.
DRAIN_ACK_KEY = "AQ_DRAIN_ACK"

#: task_metadata keys the stall ladder keeps its rung state on.  Metadata,
#: not columns: per the work-graph metadata-first rule, a new per-task
#: concept starts as a key.
META_STALL_NUDGES = "stall_nudges"
META_STALL_LAST_ACTION = "stall_last_action_at"

_LIVE_STATES = ("starting", "running", "draining")


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
        epoch: str | None = None,
    ):
        self.db = db
        self.config = config
        self.providers = providers
        self.harnesses = harnesses
        self.spec_builder = spec_builder
        self.bus = bus
        self.orchestrator = orchestrator
        self.epoch = epoch or uuid.uuid4().hex[:12]
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
            self._step_drain_ack,
            self._step_exits,
            self._step_orphans,
            self._step_stall_ladder,
            self._step_named,
            self._step_backstop,
        ):
            try:
                await step(live, now)
            except Exception:
                logger.error("Session reconciler step %s failed", step.__name__, exc_info=True)

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
        for prefix in ("s-", "n-"):
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
                logger.error("Adoption: list_running(%r) failed", prefix, exc_info=True)
                report.deferred.append(prefix)

        for name, row in by_name.items():
            if any(name.startswith(p) for p in report.deferred):
                continue
            handle = observed.get(name)
            if handle is not None and handle.instance_token == row.instance_token:
                await self.db.update_session(row.id, epoch=self.epoch)
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

        # Live sessions with no row at all: a daemon that died between
        # ``provider.start()`` and the row insert.  Design §8 asks for
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
                logger.error("Adoption: could not stop orphan session %r", name, exc_info=True)

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
            if row is not None and row.lifecycle == "task" and row.task_id:
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
            logger.error("Session reconciler: cannot list sessions", exc_info=True)
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
        nudged = await self._try_nudge(
            provider,
            row,
            f"You ran `aq session drain-ack` but task {row.task_id} is still open. "
            f"Close it first: `aq task close {row.task_id} --outcome ...`.",
        )
        if nudged:
            # Clear the ack so the next tick re-evaluates from scratch
            # rather than nudging on every cycle forever.
            try:
                await provider.set_meta(self._handle(row), DRAIN_ACK_KEY, "0")
            except Exception:
                logger.debug("could not clear drain ack on %s", row.id, exc_info=True)

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
                continue

            task = await self.db.get_task(row.task_id) if row.task_id else None
            peek = await self._peek(provider, row)
            verdict = classify_exit(
                row,
                task,
                peek,
                now=now,
                rapid_crash_window=float(self.sessions_config.restart_window_seconds),
            )
            await self._apply_verdict(provider, row, task, verdict, now)

    async def _apply_verdict(
        self,
        provider,
        row: SessionRecord,
        task,
        verdict: ExitVerdict,
        now: float,
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

        if verdict.verdict is Verdict.DRAINED:
            await self._stop_session(provider, row, reason="drained")
            return

        if verdict.verdict is Verdict.RATE_LIMIT:
            await self.db.update_session(
                row.id, state="sleeping", sleep_reason="rate_limit"
            )
            if task is not None:
                await self.db.transition_task(
                    task.id,
                    TaskStatus.PAUSED,
                    context="rate_limit",
                    resume_after=now + verdict.cooldown_seconds,
                    assigned_agent_id=None,
                )
                await self._carry_resume_key(row, task)
                await self._release_task(task)
                await self._emit(
                    "task.paused",
                    task_id=task.id,
                    project_id=task.project_id,
                    title=task.title,
                    reason="rate_limit",
                    resume_after=now + verdict.cooldown_seconds,
                )
            return

        if verdict.verdict is Verdict.RAPID_CRASH:
            count = await self.db.bump_session_restarts(row.id)
            if count >= self.sessions_config.max_restarts:
                await self._quarantine(row, task, reason="rapid_crash", now=now)
                return
            await self.db.update_session(row.id, state="stopped")
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
                await self._release_task(task)
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
        # closing.  Never silently READY: a human (or the supervisor) has
        # to look, because the work may be half-done in the worktree.
        await self.db.update_session(row.id, state="stopped")
        if task is not None:
            await self.db.set_task_meta(task.id, "needs_attention", "session_exited_open")
            await self.db.transition_task(
                task.id,
                TaskStatus.BLOCKED,
                context="session_exited_without_close",
                assigned_agent_id=None,
            )
            await self._carry_resume_key(row, task)
            await self._release_task(task)
            await self._emit(
                "task.needs_attention",
                task_id=task.id,
                project_id=task.project_id,
                title=task.title,
                reason=verdict.reason,
            )

    # -- step 4: stall ladder ---------------------------------------------

    async def _step_stall_ladder(self, live: list[SessionRecord], now: float) -> None:
        """Nudge → backoff → restart → quarantine.

        A stalled agent is not a dead agent.  Killing on timeout throws away
        the work in progress; nudging asks it to report or finish first.
        """
        ttl = float(self.sessions_config.lease_ttl_seconds)
        if ttl <= 0:
            return
        for row in live:
            if row.lifecycle != "task" or not row.task_id or row.state != "running":
                continue
            last = row.last_activity or row.started_at
            if now - last <= ttl:
                continue
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

            if rungs == 0:
                await self._emit(
                    "task.stalled",
                    task_id=row.task_id,
                    project_id=row.project_id,
                    title=task.title,
                    session_id=row.id,
                    idle_seconds=now - last,
                )

            # A provider with no input channel (subprocess) has nothing to
            # nudge *with*, so the ladder skips its nudge rungs entirely
            # rather than burning three cycles talking to no one.
            can_nudge = provider.supports(Cap.NUDGE)

            if can_nudge and rungs < self.sessions_config.stall_max_nudges:
                minutes = int((now - last) // 60)
                delivered = await self._try_nudge(
                    provider,
                    row,
                    f"No progress for {minutes} min. Report status, finish the task, "
                    f"or run `aq ask` if you are blocked.",
                )
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
            await self._release_task(task)
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
        the graceful path.  Draining without one costs nothing — the task
        is closed, and ``complete_session_task`` released the workspace and
        the agent at close time — so there is nothing left for the session
        to finish.
        """
        # (a) live session, task closed or gone.
        for row in live:
            if row.lifecycle != "task":
                continue
            if any(row.name.startswith(p) for p in self._deferred_prefixes):
                continue
            fresh = await self._still_live(row)
            if fresh is None:
                continue  # an earlier step in this tick already handled it
            row = fresh
            task = await self.db.get_task(row.task_id) if row.task_id else None
            still_open = task is not None and task.status in (
                TaskStatus.IN_PROGRESS,
                TaskStatus.ASSIGNED,
            )
            if still_open:
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
            if row.state in _LIVE_STATES:
                continue
            if any(row.name.startswith(p) for p in self._deferred_prefixes):
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
            await self._release_task(task)
            await self._emit(
                "task.needs_attention",
                task_id=task.id,
                project_id=task.project_id,
                title=task.title,
                reason="session_not_live",
            )

    # -- step 5: named desired-state ---------------------------------------

    async def _step_named(self, live: list[SessionRecord], now: float) -> None:
        """Converge persistent sessions toward the profile-declared set.

        v1 scope: *drain idle* sessions to ``sleeping``.  Starting and
        recycling named sessions needs the message routing that
        [[design/supervisor-agent]] owns, so this deliberately converges in
        one direction only rather than half-implementing wake semantics.
        """
        idle_rows = [r for r in live if r.lifecycle == "named" and r.state == "running"]
        if not idle_rows:
            return
        for row in idle_rows:
            profile = await self._profile_for(row)
            idle_timeout = int(getattr(profile, "idle_timeout", 0) or 0)
            if idle_timeout <= 0:
                continue
            last = row.last_activity or row.started_at
            if now - last <= idle_timeout:
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

    async def _profile_for(self, row: SessionRecord):
        try:
            return await self.db.get_profile(row.profile_id)
        except Exception:
            return None

    # -- step 6: backstop --------------------------------------------------

    async def _step_backstop(self, live: list[SessionRecord], now: float) -> None:
        """The final net above the ladder — not the primary defense.

        ``agents.stuck_timeout_seconds`` used to be an ``asyncio.wait_for``
        that killed work outright.  Here it only fires after the ladder has
        had its full run, and it force-kills rather than silently dropping.
        """
        limit = float(getattr(self.config.agents_config, "stuck_timeout_seconds", 0) or 0)
        if limit <= 0:
            return
        for row in live:
            if row.lifecycle != "task" or not row.task_id:
                continue
            if now - (row.started_at or now) <= limit:
                continue
            fresh = await self._still_live(row)
            if fresh is None:
                continue  # already stopped/slept/quarantined this tick
            row = fresh
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
                await self._release_task(task)
                await self._emit(
                    "task.quarantined",
                    task_id=task.id,
                    project_id=task.project_id,
                    title=task.title,
                    session_id=row.id,
                    reason="stuck_timeout",
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

    async def _release_task(self, task) -> None:
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
        """
        if task is None or self.orchestrator is None:
            return
        release = getattr(self.orchestrator, "release_session_task_resources", None)
        if release is None:
            return
        try:
            await release(task.id, agent_id=task.assigned_agent_id)
        except Exception:
            logger.error(
                "Session reconciler: releasing resources for task %s failed",
                task.id,
                exc_info=True,
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
        try:
            await self.db.set_task_meta(task.id, "session_resume_key", row.session_key)
        except Exception:
            logger.debug("could not persist resume key for task %s", task.id, exc_info=True)

    async def _try_nudge(self, provider, row: SessionRecord, text: str) -> bool:
        """Nudge, tolerating both "cannot" and "did not confirm"."""
        if not provider.supports(Cap.NUDGE):
            return False
        try:
            await provider.nudge(self._handle(row), text)
            return True
        except NotSubmitted:
            logger.info("Nudge to session %s pasted but not submitted — will retry", row.id)
            return False
        except CapabilityUnsupported:
            return False
        except Exception:
            logger.debug("nudge failed for %s", row.id, exc_info=True)
            return False

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
        fields = {"state": state}
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
            row.id, state="quarantined", quarantined_at=now, sleep_reason=reason
        )
        await self._emit(
            "session.quarantined",
            session_id=row.id,
            name=row.name,
            task_id=row.task_id,
            project_id=row.project_id,
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
            await self._release_task(task)
            await self._emit(
                "task.quarantined",
                task_id=task.id,
                project_id=task.project_id,
                title=task.title,
                session_id=row.id,
                reason=reason,
            )
