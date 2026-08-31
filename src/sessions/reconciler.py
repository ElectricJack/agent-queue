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
    NudgeDeferred,
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
        starter=None,
        epoch: str | None = None,
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
            self._step_prepare_timeout,
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
                logger.error("Adoption: list_running(%r) failed", prefix, exc_info=True)
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
            if row.lifecycle == "pool":
                # Pool sessions never send a provider-side drain ack -- an
                # idle one (no held task) marked ``desired_state="stopped"``
                # is simply done and gets torn down the pool way.
                if row.desired_state == "stopped" and row.task_id is None:
                    if self.orchestrator is None:
                        logger.warning(
                            "Pool session %s wants draining but no orchestrator is wired "
                            "— skipping", row.id,
                        )
                        continue
                    await self.orchestrator._terminate_pool_session(row, reason="drained")
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
                if s.task_id:
                    await self.db.release_claim(
                        s.id,
                        task_status=TaskStatus.READY,
                        context="prepare_timeout",
                        now=now,
                        result="prepare_failed",
                        needs_attention="prepare_timeout",
                    )
                else:
                    await self.db.update_session(s.id, claim_phase=None, claim_phase_at=None)
                if self.orchestrator is not None:
                    waiters = getattr(self.orchestrator, "claim_waiters", None) or {}
                    for key in [k for k in waiters if k[0] == s.id]:
                        fut = waiters.pop(key, None)
                        if fut is not None and not fut.done():
                            fut.set_result("prepare_failed")
                await self._emit(
                    "session.claim_timeout", session_id=s.id, task_id=s.task_id
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
        # closing.  Never silently READY: a human (or the supervisor) has
        # to look, because the work may be half-done in the worktree.
        await self.db.update_session(row.id, state="stopped", desired_state="stopped",
                                     ended_at=now, end_reason="session_exited_open")
        if task is not None:
            await self.db.set_task_meta(task.id, "needs_attention", "session_exited_open")
            await self.db.transition_task(
                task.id,
                TaskStatus.BLOCKED,
                context="session_exited_without_close",
                assigned_agent_id=None,
            )
            await self._carry_resume_key(row, task)
            await self._release_task(task, row, reason="session_exited_open")
            await self._emit(
                "task.needs_attention",
                task_id=task.id,
                project_id=task.project_id,
                title=task.title,
                reason=verdict.reason,
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
        if verdict.verdict is Verdict.RAPID_CRASH:
            orch._pool_quarantine[(row.project_id, row.profile_id)] = (
                now + self.sessions_config.restart_window_seconds
            )
        elif verdict.verdict is Verdict.RATE_LIMIT:
            await self._apply_rate_limit_cooldown(row)
            # Task stays READY (the default ``_terminate_pool_session``
            # applies) -- it is the *pool key*, not this task, that is
            # rate-limited, so a different worker should pick it straight
            # back up.  The window matches whatever ``_step_exits`` handed
            # ``classify_exit`` for this verdict.
            orch._pool_quarantine[(row.project_id, row.profile_id)] = (
                now + verdict.cooldown_seconds
            )
        await orch._terminate_pool_session(row, reason=verdict.verdict.name.lower())

    async def _waiting_for_question(self, row, now):
        service = getattr(self.orchestrator, "agent_questions", None)
        if service is None:
            return False
        return await service.is_waiting(row, now=now)

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
            if row.lifecycle not in ("task", "pool") or not row.task_id or row.state != "running":
                continue
            if row.lifecycle == "pool" and row.claim_phase != "active":
                continue
            if await self._waiting_for_question(row, now):
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

            # A provider with no input channel (subprocess) has nothing to
            # nudge *with*, so the ladder skips its nudge rungs entirely
            # rather than burning three cycles talking to no one.
            can_nudge = provider.supports(Cap.NUDGE)

            nudge_due = can_nudge and rungs < self.sessions_config.stall_max_nudges
            delivered: bool | None = False
            if nudge_due:
                minutes = int((now - last) // 60)
                delivered = await self._try_nudge(
                    provider,
                    row,
                    f"No progress for {minutes} min. Report status, finish the task, "
                    "or report a blocker with "
                    '`aq message send --to user --project "$AQ_PROJECT_ID" '
                    '--body "Blocked: <question>"`.',
                )
                if delivered is None:
                    # The input belongs to the user, or cannot be inspected.
                    # Waiting for an empty composer is not a failed attempt.
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
        the graceful path.  Draining without one costs nothing — the task
        is closed, and ``complete_session_task`` released the workspace and
        the agent at close time — so there is nothing left for the session
        to finish.
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
            task = await self.db.get_task(row.task_id) if row.task_id else None
            still_open = task is not None and task.status in (
                TaskStatus.IN_PROGRESS,
                TaskStatus.ASSIGNED,
            )
            if still_open:
                continue
            if row.lifecycle == "pool":
                if self.orchestrator is None:
                    logger.warning(
                        "Pool session %s is orphaned but no orchestrator is wired "
                        "— skipping", row.id,
                    )
                    continue
                await self.orchestrator._terminate_pool_session(
                    row,
                    reason="orphaned",
                    task_status=task.status if task is not None else TaskStatus.READY,
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
        except Exception:
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
            if row.lifecycle == "pool":
                last = row.last_activity if row.last_activity is not None else row.started_at
                elapsed = now - (last or now)
            else:
                baseline = row.started_at
                questions = getattr(self.orchestrator, "agent_questions", None)
                if questions is not None:
                    resumed_at = await questions.backstop_activity_at(row)
                    if resumed_at is not None:
                        baseline = resumed_at
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
        release = getattr(self.orchestrator, "release_session_task_resources", None)
        if release is None:
            return
        try:
            await release(task.id, agent_id=task.assigned_agent_id, expect_claim_epoch=task.claim_epoch)
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
        if row.harness == "codex" and row.session_key == row.id:
            return  # Legacy AQ UUID placeholders are not Codex resume identities.
        try:
            await self.db.set_task_meta(task.id, "session_resume_key", row.session_key)
        except Exception:
            logger.debug("could not persist resume key for task %s", task.id, exc_info=True)

    async def _try_nudge(self, provider, row: SessionRecord, text: str) -> bool | None:
        """True for delivery, False for failure, None for untouched input."""
        if not provider.supports(Cap.NUDGE):
            return False
        try:
            await provider.nudge(self._handle(row), text)
            return True
        except NudgeDeferred:
            logger.debug("Nudge to session %s deferred; terminal input untouched", row.id)
            return None
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
            state="quarantined",
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
