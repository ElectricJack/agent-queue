"""Monitoring mixin — state checks run each cycle."""

from __future__ import annotations

import logging
import time

from src.discord.notifications import (
    format_failed_blocked_report,
    format_failed_blocked_report_embed,
)
from src.notifications.builder import build_task_detail
from src.notifications.events import (
    BudgetWarningEvent,
    ChainStuckEvent,
    StuckDefinedTaskEvent,
)
from src.models import DepType, Task, TaskStatus
from src.database.queries.hierarchy_queries import CONTAINER_KEY
from src.database.queries.task_queries import TERMINAL_BLOCKED_META_KEY
from src.task_summary import write_task_summary

logger = logging.getLogger(__name__)

# Edge kinds the pre-work-graph readiness scan has no rule for.  A task
# carrying one is deferred to the ``is_blocked`` projection rather than
# guessed at (see ``_legacy_promotion_decisions``).
_LEGACY_UNKNOWN_DEP_TYPES = frozenset({DepType.WAITS_FOR.value, DepType.CONDITIONAL_BLOCKS.value})


class MonitoringMixin:
    """Monitoring and housekeeping methods mixed into Orchestrator."""

    async def _resume_paused_tasks(self) -> None:
        """Check PAUSED tasks whose ``resume_after`` has elapsed and promote to READY.

        Tasks enter PAUSED when the agent hits a rate limit or token
        exhaustion, with ``resume_after`` set to a future timestamp.
        This method scans all PAUSED tasks and transitions any whose
        backoff timer has expired back to READY for re-scheduling.

        ``PAUSED`` with ``resume_after IS NULL`` is the operator-hold
        sentinel (``task_queries._not_manually_paused``): every lifecycle
        write is fenced on it, so such a task can only leave PAUSED through
        ``resume_task``.  A hold always carries a ``manual_pause`` metadata
        snapshot, written in the same transaction.  One *without* that
        snapshot is therefore not a hold at all — it is a task wedged by a
        crash or a partial write, and nothing else will ever look at it
        again.  Recover it here (this cascade runs every cycle, so a daemon
        restart picks it up too) rather than leaving it queued forever.

        Tasks whose timer has *not* expired are handed to
        :meth:`_resume_slot_starved_tasks`, which ends the one wait a timer
        is the wrong instrument for: waiting on a worktree slot.
        """
        paused = await self.db.list_tasks(status=TaskStatus.PAUSED)
        now = time.time()
        still_paused: list[Task] = []
        for task in paused:
            if task.resume_after is None:
                snapshot = await self.db.get_task_meta(task.id, "manual_pause")
                if snapshot is None:
                    await self._recover_orphaned_pause(task)
                    continue
                if isinstance(snapshot, dict) and snapshot.get("cleanup_pending"):
                    try:
                        await self.retry_manual_pause_cleanup(task.id)
                    except Exception:
                        self._log_pause_cleanup_retry(task.id)
                    else:
                        self._pause_cleanup_retries().pop(task.id, None)
                continue
            if task.resume_after and task.resume_after <= now:
                self._slot_starved_pauses.pop(task.id, None)
                await self.db.transition_task(
                    task.id,
                    TaskStatus.READY,
                    context="resume_paused",
                    assigned_agent_id=None,
                    resume_after=None,
                )
                # The backoff is over and the task is back on the frontier.
                # A ``needs_attention`` flag written with the pause (the
                # session reconciler's exit-without-close leg) has done its
                # job — the durable task comment is the incident trail —
                # and left in place it would mark a running task as needing
                # attention, keep it out of BLOCKED re-promotion, and turn
                # any later BLOCKED into a false recovery incident.  The
                # supervisor's retry decision clears it the same way.
                await self.db.delete_task_meta(task.id, "needs_attention")
                continue
            still_paused.append(task)

        await self._resume_slot_starved_tasks(still_paused)

    async def _resume_slot_starved_tasks(self, paused: list[Task]) -> None:
        """Cut the backoff short for tasks waiting only on a worktree slot.

        A task that fails to acquire a slot is PAUSED with a backoff timer,
        which makes it invisible to the scheduler until the timer expires.
        That is right for a wait nothing can shorten, and wrong for this one:
        the scheduler orders READY tasks by ``(priority, id)``, so a task
        sitting out the backoff loses every slot that frees during it to
        whatever lower-priority task happens to be READY.  Observed live
        (2026-09-02): a priority-3 merge sweep sat in that loop for ~40
        minutes behind a steady inflow of priority-30 work, repeatedly paying
        for slot growth and then losing the slot it provisioned.

        So: as soon as the project has an acquirable slot again, every task
        parked on that wait goes back to READY in the same cycle and priority
        picks the winner.  The losers simply re-park — the same outcome the
        expired backoff would have produced, minus the starvation.

        Only slot waits qualify (``_SLOT_WAIT_REASONS``).  A branch-held or
        clone-exhausted wait is *not* resolved by a free slot, and resuming
        those early would turn their operator notices into per-cycle spam.
        """
        if not self._slot_starved_pauses:
            return
        candidates = [t for t in paused if t.id in self._slot_starved_pauses]
        # Nothing else will clear an entry whose task has left PAUSED.
        paused_ids = {t.id for t in paused}
        for task_id in list(self._slot_starved_pauses):
            if task_id not in paused_ids:
                del self._slot_starved_pauses[task_id]
        if not candidates:
            return

        free_by_project: dict[str, int] = {}
        for task in candidates:
            pid = task.project_id
            if pid not in free_by_project:
                project = await self.db.get_project(pid)
                free_by_project[pid] = await self.db.count_free_slots(
                    pid, worktree_slot_cap=self._project_slot_cap(project)
                )
            if free_by_project[pid] <= 0:
                continue
            del self._slot_starved_pauses[task.id]
            logger.info(
                "Task %s (priority %s) resumed early — a worktree slot came "
                "free in project %s; priority decides the next dispatch",
                task.id,
                task.priority,
                pid,
            )
            await self.db.transition_task(
                task.id,
                TaskStatus.READY,
                context="resume_slot_free",
                assigned_agent_id=None,
                resume_after=None,
            )

    def _pause_cleanup_retries(self) -> dict[str, int]:
        """Per-task count of consecutive failed pause-cleanup retries."""
        counts = getattr(self, "_pause_cleanup_retry_counts", None)
        if counts is None:
            counts = {}
            self._pause_cleanup_retry_counts = counts
        return counts

    def _log_pause_cleanup_retry(self, task_id: str) -> None:
        """Report a failed retry without emitting a line every 5s forever.

        Checkpoint failures now converge on their own, but a session or
        adapter that refuses to stop is deliberately retried indefinitely —
        releasing its resources while the process still runs would be worse
        than waiting.  Keep that visible without drowning the log: the first
        few failures and then every tenth warn, the rest are debug.
        """
        counts = self._pause_cleanup_retries()
        counts[task_id] = attempts = counts.get(task_id, 0) + 1
        loud = attempts <= 3 or attempts % 10 == 0
        logger.log(
            logging.WARNING if loud else logging.DEBUG,
            "Task %s remains paused; stop cleanup will retry (attempt %d)",
            task_id,
            attempts,
            exc_info=loud,
        )

    async def _recover_orphaned_pause(self, task) -> None:
        """Return a task wedged in PAUSED with no operator hold to READY.

        The resume path is the only writer that can move it: the manual-pause
        fence rejects ``transition_task`` for exactly this state.  With no
        ``manual_pause`` snapshot to restore, it resumes to READY and clears
        the stale assignment — which is what the interrupted pause would have
        produced had it completed.

        The scan read the status and the snapshot in two separate
        transactions, so an operator hold committing between them looks
        exactly like a wedge from here — and a fresh hold is precisely what
        an unconditional ``resume_task`` would tear down.  So the write is
        ``recover_orphaned_pause``, which re-checks the status, the timer and
        the snapshot under the task-row lock in the same transaction as the
        transition and declines when any of them changed.
        """
        try:
            recovered = await self.db.recover_orphaned_pause(task.id)
        except Exception:
            logger.warning(
                "Could not recover orphaned pause on task %s", task.id, exc_info=True
            )
            return
        if recovered is None:
            logger.debug(
                "Task %s left PAUSED: a hold or a resume timer landed after the scan",
                task.id,
            )
            return
        logger.warning(
            "Task %s was PAUSED with no resume timer and no operator hold — "
            "resumed it; nothing else would have scheduled it again",
            task.id,
        )

    async def _check_defined_tasks(self) -> None:
        """Promote DEFINED/BLOCKED tasks to READY when the graph allows it.

        Two independent deciders run every cycle (work-graph implementation
        spec §6.2):

        * **legacy** — the historical per-task dependency scan, including the
          ``is_plan_subtask`` special case;
        * **projection** — one indexed read of ``tasks.is_blocked``, the
          persisted blocked-state projection.

        Which one *acts* is the ``work_graph.blocked_state_authoritative``
        flag; the other still runs and any disagreement is logged.  That is
        shadow mode (§9): the projection earns authority only after an
        observation window with zero divergence, and rollback is a config
        flip because the legacy scan stays in the tree.

        Conditional-edge disposal runs first so a contingency task that can
        never fire is closed rather than considered for promotion.

        This runs after the gate sweep so freshly-resolved gates can
        unblock their dependents in the same cycle.
        """
        await self._close_dead_conditional_tasks()

        defined = await self.db.list_tasks(status=TaskStatus.DEFINED)
        # Also check BLOCKED tasks — their dependencies may have been
        # satisfied since they were blocked, allowing them to proceed.
        blocked = await self.db.list_tasks(status=TaskStatus.BLOCKED)
        # Session failures and timeouts require explicit attention even when
        # their old graph dependencies are satisfied. Otherwise this cascade
        # immediately undoes the reconciler's BLOCKED/quarantine decision.
        blocked = [
            task for task in blocked
            if not await self.db.get_task_meta(task.id, "needs_attention")
        ]
        # A terminal close (hard failure, retry budget spent, pipeline stop,
        # timeout, operator stop) is BLOCKED by decision, not by the graph.
        # Neither decider may recover it: the projection clearing says
        # nothing about the failure, and every child of a container carries
        # a ``parent-child`` edge, so "has a blocking edge" cannot tell a
        # hard-failed child from a graph-blocked one (crisp-pinnacle-54).
        # Only an explicit restart/reopen — which clears the mark — brings
        # the task back.
        terminal = await self.db.task_ids_with_meta(
            [task.id for task in blocked], TERMINAL_BLOCKED_META_KEY
        )
        blocked = [task for task in blocked if task.id not in terminal]

        legacy, deferred = await self._legacy_promotion_decisions(defined, blocked)
        projected = await self._projected_promotion_decisions(defined, blocked)

        authoritative = self.config.work_graph.blocked_state_authoritative
        self._log_promotion_divergence(legacy, projected, deferred, authoritative)

        if authoritative:
            decisions = projected
        else:
            # The legacy scan predates typed edges and cannot judge a task
            # that carries one; for those it defers to the projection.  Its
            # verdict still rules every classic `blocks`-only graph, which is
            # what the shadow window is actually evidence about.
            decisions = dict(legacy)
            for task_id in deferred:
                if task_id in projected:
                    decisions[task_id] = projected[task_id]

        for task_id in sorted(decisions):
            flipped = await self.db.transition_task(
                task_id, TaskStatus.READY, context=decisions[task_id]
            )
            # WG-4: bus emit for the flipped set so ``task.blocked`` /
            # ``task.unblocked`` triggers actually fire.  ``log_blocked_flips``
            # already handles the audit row.
            if flipped:
                await self._emit_blocked_flips(flipped, reason="promotion")

        # A promoted container is released straight on to IN_PROGRESS (no
        # agent) so it never sits in the READY frontier where the scheduler
        # or a pool claim would hand it to a worker (spec §7, calm-ember-48).
        await self._release_ready_containers(sorted(decisions))

    async def _release_ready_containers(self, candidate_ids) -> set[str]:
        """Move every flagged container among *candidate_ids* READY → IN_PROGRESS.

        A container (``task_metadata.container``, spec §7) has no deliverable
        of its own: it exists to settle once its children complete.  Dispatching
        it is self-defeating — Invariant 6 refuses the worker's close while a
        child is open, and the worker's own live session is exactly what the
        settlement predicate waits on — so the worker idles holding an agent
        slot and the project-repo lock for the whole subtree's lifetime
        (solid-harbor.65, 2026-09-03).

        Release lands the container in the same shape ``creator.PARENT_STATUS``
        births graph containers in and recovery preserves: IN_PROGRESS with no
        agent, which (a) satisfies the children's ``parent-child`` edge and
        (b) is the status settlement acts on.  A container whose children are
        already all COMPLETED (or that has none) settles here and now rather
        than at the next backstop sweep.

        Returns every flagged id, whether or not its transition landed, so a
        caller can withhold the lot from dispatch in the same cycle.
        """
        ids = sorted({tid for tid in candidate_ids if tid})
        if not ids:
            return set()
        flagged = await self.db.task_ids_with_meta(ids, CONTAINER_KEY)
        for task_id in sorted(flagged):
            task = await self.db.get_task(task_id)
            # Only READY is ours to move: a claim or a session may have taken
            # it in the meantime, and IN_PROGRESS is already the target.
            if task is None or task.status != TaskStatus.READY:
                continue
            try:
                flipped = await self.db.transition_task(
                    task_id,
                    TaskStatus.IN_PROGRESS,
                    context="container_released",
                    assigned_agent_id=None,
                )
            except Exception:
                logger.exception("Failed to release container task '%s'", task_id)
                continue
            logger.info(
                "Released container task '%s' (%s) to IN_PROGRESS without an agent — "
                "it settles when its children finish",
                task_id,
                task.title,
            )
            if flipped:
                await self._emit_blocked_flips(flipped, reason="promotion")
            await self._settle_seeds({task_id})
        return flagged

    async def _legacy_promotion_decisions(
        self,
        defined: list[Task],
        blocked: list[Task],
    ) -> tuple[dict[str, str], set[str]]:
        """The pre-projection readiness scan, as a pure decision function.

        Returns ``({task_id: transition_context}, deferred_ids)``.  Preserved
        verbatim (bar the compute/apply split) so it can act as the
        shadow-mode oracle:

        - DEFINED with no dependencies → READY;
        - DEFINED/BLOCKED whose every blocking dependency is COMPLETED → READY;
        - plan subtasks with an IN_PROGRESS parent count the parent
          dependency as satisfied (the special case the ``parent-child``
          edge type generalises away).

        The scan only ever knew two shapes: ``blocks`` edges, and the
        plan-subtask parent edge.  A task carrying an edge kind it predates
        (``waits-for``, ``conditional-blocks``, or ``parent-child`` outside a
        plan subtask) is **deferred** — reported in the second return value
        and left to the projection.  Deferring rather than guessing matters:
        ``are_dependencies_met`` would read a ``conditional-blocks`` edge to a
        COMPLETED dependency as *satisfied* and run a contingency task whose
        condition never fired.

        Deciding before applying is safe: every promotion here targets READY,
        and READY never satisfies another task's dependency.
        """
        decisions: dict[str, str] = {}
        deferred: set[str] = set()
        for task in [*defined, *blocked]:
            typed_edges = await self.db.get_typed_dependencies(task.id)
            is_plan_child = bool(task.is_plan_subtask and task.parent_task_id)
            if any(
                dep_type in _LEGACY_UNKNOWN_DEP_TYPES
                or (
                    dep_type == DepType.PARENT_CHILD.value
                    and not (is_plan_child and target == task.parent_task_id)
                )
                for target, dep_type in typed_edges
            ):
                deferred.add(task.id)
                continue

            # Plan subtask special handling: the parent plan transitions to
            # IN_PROGRESS (not COMPLETED) when approved, so standard
            # are_dependencies_met() would block forever.  We treat the
            # IN_PROGRESS parent dep as satisfied.
            if task.is_plan_subtask and task.parent_task_id:
                parent = await self.db.get_task(task.parent_task_id)
                if parent and parent.status == TaskStatus.IN_PROGRESS:
                    # Parent plan is approved and active — treat parent dep as met.
                    # Check only non-parent dependencies.
                    deps = await self.db.get_dependencies(task.id)
                    non_parent_deps = deps - {task.parent_task_id}
                    all_met = True
                    for dep_id in non_parent_deps:
                        dep_task = await self.db.get_task(dep_id)
                        if not dep_task or dep_task.status != TaskStatus.COMPLETED:
                            all_met = False
                            break
                    if all_met:
                        decisions[task.id] = "deps_met_plan_parent_active"
                    continue

            deps = await self.db.get_dependencies(task.id)
            if not deps:
                if task.status == TaskStatus.DEFINED:
                    # No dependencies — promote DEFINED to READY.
                    # (BLOCKED tasks with no deps stay blocked — they were
                    # blocked for other reasons like verification failure.)
                    decisions[task.id] = "deps_met_no_deps"
            else:
                if await self.db.are_dependencies_met(task.id):
                    decisions[task.id] = "deps_met"
        return decisions, deferred

    async def _projected_promotion_decisions(
        self,
        defined: list[Task],
        blocked: list[Task],
    ) -> dict[str, str]:
        """Promotion straight off the ``is_blocked`` projection (design §4.4).

        - ``DEFINED ∧ is_blocked = 0`` → READY.
        - ``BLOCKED ∧ is_blocked = 0`` → READY **only** when the task carries
          at least one blocking edge or gate; a failure-BLOCKED task with no
          graph blocker stays put.  (A terminal close *with* a graph edge —
          any child of a container — is filtered out of ``blocked`` before
          this runs; see ``_check_defined_tasks``.)
        """
        decisions: dict[str, str] = {task.id: "deps_met" for task in defined if not task.is_blocked}

        recoverable = [task.id for task in blocked if not task.is_blocked]
        if recoverable:
            with_blockers = await self.db.tasks_with_graph_blockers(recoverable)
            for task_id in recoverable:
                if task_id in with_blockers:
                    decisions[task_id] = "deps_met"
        return decisions

    def _log_promotion_divergence(
        self,
        legacy: dict[str, str],
        projected: dict[str, str],
        deferred: set[str],
        authoritative: bool,
    ) -> None:
        """Log where the two deciders disagree.

        The observation gate on flipping ``blocked_state_authoritative``: a
        window of clean cycles is the evidence that the projection matches
        the scan it replaces.  Only the *sets* are compared — the transition
        context strings are cosmetic.

        Tasks the legacy scan deferred on (typed edges it predates) are
        excluded from the comparison: there is no second opinion to compare
        against, so counting them would drown the signal the observation
        window is looking for.  Their **count** is still reported, at INFO —
        without it "zero divergence for a week" cannot be told apart from
        "the oracle judged nothing all week".

        Logging is edge-triggered.  This runs every 5 s, and a divergence
        persists until someone acts on it: one stuck plan parent re-logged at
        WARNING 17 000 times a day buries the very signal the window exists to
        collect.  A line is emitted only when the reported state changes, so
        each distinct divergence appears once, and its clearing appears once.
        """
        only_legacy = tuple(sorted(set(legacy) - set(projected) - deferred))
        only_projected = tuple(sorted(set(projected) - set(legacy) - deferred))

        state = (only_legacy, only_projected, len(deferred))
        if state == getattr(self, "_last_divergence_state", None):
            return
        self._last_divergence_state = state

        logger.info(
            "blocked-state shadow: %d deferred to the projection, "
            "%d legacy-only, %d projection-only",
            len(deferred),
            len(only_legacy),
            len(only_projected),
        )
        if not only_legacy and not only_projected:
            return
        logger.warning(
            "blocked-state divergence (%s authoritative): legacy-only=%s projection-only=%s",
            "projection" if authoritative else "legacy scan",
            list(only_legacy) or "-",
            list(only_projected) or "-",
        )

    async def _close_dead_conditional_tasks(self) -> None:
        """Dispose of contingency tasks whose condition can never fire.

        A ``conditional-blocks`` edge is satisfied only by the dependency's
        *terminal failure*.  Once that dependency reaches COMPLETED the edge
        is permanently unsatisfiable, so a task whose only remaining blocker
        is such an edge would sit in the queue forever.  It is closed as a
        no-op instead (work-graph design §3.1).

        Gated on ``work_graph.conditional_autoclose`` (default on).  It is
        deliberately independent of ``blocked_state_authoritative``: the
        "dead conditional edge" test is a direct graph fact, not a read of
        the projection, and ``conditional-blocks`` edges only exist where
        someone explicitly created one.
        """
        if not self.config.work_graph.conditional_autoclose:
            return

        try:
            dead = await self.db.find_dead_conditional_tasks()
        except Exception:
            logger.error("conditional auto-close: query failed", exc_info=True)
            return

        for task_id, project_id in dead:
            try:
                await self.db.set_task_meta(task_id, "work_outcome", "no-op")
                await self.db.transition_task(
                    task_id, TaskStatus.COMPLETED, context="conditional_dep_completed"
                )
                await self.db.log_event(
                    "task.skipped_conditional",
                    project_id=project_id,
                    task_id=task_id,
                    payload="conditional-blocks dependency completed — contingency not needed",
                )
                logger.info("Task %s auto-closed: its conditional dependency completed", task_id)
            except Exception:
                logger.error("conditional auto-close: failed to close %s", task_id, exc_info=True)

    def register_settlement_listener(self) -> None:
        """Wire ``db.transition_task``'s post-commit settlement callback to us."""
        self.db.set_settlement_listener(self._on_containers_settled)
        self.db.set_ready_listener(self._on_frontier_entries)

    async def _on_frontier_entries(self, entries: list[tuple[str, str]]) -> None:
        """Post-commit fan-out for every entry into the ready frontier (spec §9)."""
        for task_id, reason in entries:
            task = await self.db.get_task(task_id)
            if task is None:
                continue
            try:
                await self._emit_task_event("task.ready", task, reason=reason)
            except Exception:
                logger.exception("task.ready emit failed for %s", task_id)

    async def _on_containers_settled(self, ids: list[str]) -> None:
        """Post-commit fan-out for containers completed by settlement (spec §7).

        Everything the old per-tick scan did after the transition: bus event,
        operator notification, vault summary, workflow-stage check.

        The whole per-container body is wrapped: one container's notification
        or stage check blowing up must not cost every container after it in
        the same batch its fan-out.
        """
        for cid in ids:
            try:
                task = await self.db.get_task(cid)
                if task is None:
                    continue
                try:
                    await self._emit_task_event("task.completed", task)
                except Exception:
                    logger.exception("task.completed emit failed for container %s", cid)
                try:
                    result = await self.db.get_task_result(cid)
                    write_task_summary(self.config.vault_root, task, result)
                except Exception as e:
                    logger.warning("Failed to write task summary for %s: %s", cid, e)
                await self._emit_text_notify(
                    f"**Container completed:** `{task.id}` — {task.title} (all children finished).",
                    project_id=task.project_id,
                )
                await self._check_workflow_stage_completion(task)
            except Exception:
                logger.exception("settlement fan-out failed for container %s", cid)

    _last_container_sweep: float = 0.0

    async def _sweep_container_completion(self) -> None:
        """Low-cadence backstop for the event-driven settlement (spec §7)."""
        interval = self.config.work_graph.container_sweep_interval_seconds
        if interval <= 0:
            return
        now = time.time()
        if now - self._last_container_sweep < interval:
            return
        self._last_container_sweep = now
        candidates = await self.db.settle_candidates()
        if not candidates:
            return
        settled = await self._settle_seeds(set(candidates))
        for cid in settled:
            logger.warning("container settlement backstop hit: %s (event path missed it)", cid)

    async def _settle_seeds(self, seeds: set[str]) -> list[str]:
        """Run the §7 settlement predicate over *seeds* now, with post-commit fan-out.

        The same commit-then-notify shape as ``transition_task``: blocked-state
        flips are logged, settled containers reach the settlement listener, and
        waiters the settlement released are announced to the ready listener.
        """
        if not seeds:
            return []
        async with self.db._engine.begin() as conn:
            result = await self.db.settle_containers(set(seeds), conn=conn)
        await self.db.log_blocked_flips(result.flipped)
        await self.db._notify_settled(result.settled)
        await self.db._notify_ready(result.ready)
        return list(result.settled)

    async def _check_stuck_defined_tasks(self) -> None:
        """Monitoring: detect DEFINED tasks stuck waiting for dependencies.

        Queries for tasks that have been in DEFINED status longer than
        ``monitoring.stuck_task_threshold_seconds`` and sends a notification
        with details about which upstream dependencies are blocking them.

        Notifications are rate-limited to one per threshold period per task
        (tracked in ``_stuck_notified_at``) to avoid flooding Discord.
        The tracker is garbage-collected each cycle to remove entries for
        tasks that are no longer stuck.
        """
        threshold = self.config.monitoring.stuck_task_threshold_seconds
        if threshold <= 0:
            return  # Disabled

        stuck_tasks = await self.db.get_stuck_defined_tasks(threshold)
        if not stuck_tasks:
            return

        now = time.time()

        # Clean up notification tracker for tasks no longer DEFINED
        stuck_ids = {t.id for t in stuck_tasks}
        for tid in list(self._stuck_notified_at):
            if tid not in stuck_ids:
                del self._stuck_notified_at[tid]

        for task in stuck_tasks:
            # Rate-limit: only notify once per threshold period per task
            last_notified = self._stuck_notified_at.get(task.id, 0)
            if now - last_notified < threshold:
                continue

            # Find which dependencies are blocking this task
            blocking = await self.db.get_blocking_dependencies(task.id)

            # Calculate how long the task has been stuck
            task_created_at = await self.db.get_task_created_at(task.id)
            if not task_created_at:
                task_created_at = now  # fallback (should not happen)
            stuck_hours = (now - task_created_at) / 3600

            await self._emit_notify(
                "notify.stuck_defined_task",
                StuckDefinedTaskEvent(
                    task=build_task_detail(task),
                    blocking_deps=[
                        {
                            "id": dep_id,
                            "title": dep_title,
                            "status": dep_status,
                            "dep_type": dep_type,
                            "project_id": dep_project_id,
                        }
                        for dep_id, dep_title, dep_status, dep_type, dep_project_id in blocking
                    ],
                    stuck_hours=stuck_hours,
                    project_id=task.project_id,
                ),
            )

            # Log the event
            blocking_info = ", ".join(
                f"{dep_id}({dep_status}, {dep_type})"
                for dep_id, _, dep_status, dep_type, _ in blocking[:10]
            )
            await self.db.log_event(
                "stuck_defined_task",
                project_id=task.project_id,
                task_id=task.id,
                payload=f"stuck_hours={stuck_hours:.1f}, blocking=[{blocking_info}]",
            )
            logger.info(
                "Stuck task detected: %s — %s (DEFINED for %.1fh, blocked by %d deps)",
                task.id,
                task.title,
                stuck_hours,
                len(blocking),
            )

            self._stuck_notified_at[task.id] = now

    async def _check_failed_blocked_tasks(self) -> None:
        """Periodic report: summarize all FAILED and BLOCKED tasks to the channel.

        Queries for tasks currently in FAILED or BLOCKED status and posts a
        consolidated summary to the notification channel so operators have an
        at-a-glance view of everything needing manual intervention.

        Rate-limited by ``monitoring.failed_blocked_report_interval_seconds``
        (default 1 hour).  Set to 0 or negative to disable.  The report is
        only sent when at least one task is in FAILED or BLOCKED status.
        """
        interval = self.config.monitoring.failed_blocked_report_interval_seconds
        if interval <= 0:
            return  # Disabled

        now = time.time()
        if now - self._last_failed_blocked_report < interval:
            return

        self._last_failed_blocked_report = now

        failed_tasks = await self.db.list_tasks(status=TaskStatus.FAILED)
        blocked_tasks = await self.db.list_tasks(status=TaskStatus.BLOCKED)

        if not failed_tasks and not blocked_tasks:
            return

        total = len(failed_tasks) + len(blocked_tasks)
        logger.info(
            "Failed/blocked report: %d failed, %d blocked (%d total)",
            len(failed_tasks),
            len(blocked_tasks),
            total,
        )

        # Group tasks by project so we can notify each project's channel
        projects: dict[str, tuple[list, list]] = {}
        for t in failed_tasks:
            projects.setdefault(t.project_id, ([], []))[0].append(t)
        for t in blocked_tasks:
            projects.setdefault(t.project_id, ([], []))[1].append(t)

        for project_id, (proj_failed, proj_blocked) in projects.items():
            msg = format_failed_blocked_report(proj_failed, proj_blocked)
            format_failed_blocked_report_embed(proj_failed, proj_blocked)
            await self._emit_text_notify(msg, project_id=project_id)

    async def _auto_archive_tasks(self) -> None:
        """Automatically archive terminal tasks older than the configured threshold.

        Runs at most once per hour (rate-limited via ``_last_auto_archive``)
        and only when ``config.archive.enabled`` is True.  Tasks matching the
        configured terminal statuses whose ``updated_at`` is older than
        ``archive.after_hours`` are silently moved to the ``archived_tasks``
        table so they no longer appear in active views.

        This eliminates the need for agents or operators to manually run
        ``/archive-tasks``; the orchestrator handles it automatically.
        """
        archive_cfg = self.config.archive
        if not archive_cfg.enabled:
            return

        now = time.time()
        # Rate-limit to once per hour
        if now - self._last_auto_archive < 3600:
            return
        self._last_auto_archive = now

        older_than_seconds = archive_cfg.after_hours * 3600
        try:
            archived_ids = await self.db.archive_old_terminal_tasks(
                statuses=archive_cfg.statuses,
                older_than_seconds=older_than_seconds,
            )
        except Exception as e:
            logger.error("Auto-archive error: %s", e)
            return

        if archived_ids:
            logger.info(
                "Auto-archived %d terminal task(s) older than %.1fh: %s%s",
                len(archived_ids),
                archive_cfg.after_hours,
                ", ".join(archived_ids[:10]),
                "..." if len(archived_ids) > 10 else "",
            )
            for tid in archived_ids:
                try:
                    await self.db.log_event(
                        "task_auto_archived",
                        task_id=tid,
                    )
                except Exception:
                    pass

    async def _check_paused_playbook_timeouts(self) -> None:
        """Sweep paused playbook runs for expired timeouts (roadmap 5.4.4).

        Delegates to :meth:`CommandHandler.check_paused_playbook_timeouts`
        which resolves per-node and per-playbook timeout configuration and
        handles the transition (either to a timeout node or to timed_out
        status).

        Runs every tick (5s) — the query is lightweight (indexed status
        column) and the actual timeout handling only fires for runs that
        have genuinely expired.

        No-op while the playbook subsystem is paused
        (``playbooks.enabled=false``) — paused ``playbook_runs`` rows are left
        exactly as they are: they neither resume nor time out.  See
        docs/specs/implementation/feature-pauses.md P9.
        """
        if not self.config.playbooks.enabled:
            return
        if not hasattr(self, "command_handler") or self.command_handler is None:
            return
        try:
            results = await self.command_handler.check_paused_playbook_timeouts()
            for r in results:
                logger.info(
                    "Playbook run %s timed out: status=%s, timeout=%ds, on_timeout=%s",
                    r["run_id"],
                    r["status"],
                    r["timeout_seconds"],
                    r.get("on_timeout"),
                )
        except Exception as e:
            logger.warning("Paused playbook timeout sweep failed: %s", e)

    async def _sweep_playbook_v2_retention(self) -> None:
        """Collect aged-out Playbook V2 state (durable-state child plan §12.2).

        Three guards, in the order that makes the common case free:

        * ``playbooks.enabled`` is false by default, so on a stock
          install this returns before touching the clock;
        * the sweep runs at most once per
          ``playbooks.v2_retention_sweep_interval_seconds`` (default an
          hour), throttled by ``_last_playbook_retention_sweep`` the same way
          ``_last_log_cleanup`` throttles log cleanup;
        * every failure is swallowed with a warning.  A retention sweep is
          pure housekeeping — letting it abort the scheduler cycle would turn
          "an artifact file is unreadable" into "the daemon stopped
          dispatching", which is precisely the trade the try/except exists to
          refuse.

        The throttle stamp is advanced *before* the sweep runs, so a sweep
        that raises does not retry every 5 s for an hour.
        """
        playbooks = self.config.playbooks
        if not getattr(playbooks, "enabled", False):
            return
        now = time.time()
        interval = max(int(getattr(playbooks, "v2_retention_sweep_interval_seconds", 3600)), 1)
        if now - self._last_playbook_retention_sweep < interval:
            return
        self._last_playbook_retention_sweep = now
        try:
            from src.playbooks.retention import ArtifactRetentionSweeper

            sweeper = ArtifactRetentionSweeper(
                self.db, playbooks, self.config.compiled_root
            )
            await sweeper.sweep(now)
        except Exception as e:
            logger.warning("Playbook V2 retention sweep failed: %s", e)

    async def _sweep_operational_event_retention(self) -> None:
        """Collect expired terminal onboarding requests once per hour.

        These records are durable idempotency state, not playbook state, so
        their retention must run even when the playbook subsystem is disabled.
        """
        now = time.time()
        if now - self._last_operational_event_retention_sweep < 3600:
            return
        self._last_operational_event_retention_sweep = now
        try:
            days = int(self.config.events.onboarding_request_retention_days)
            removed = await self.db.purge_finished_onboarding_requests(
                now - days * 86_400.0
            )
            if removed:
                logger.info("Onboarding request retention: removed %d terminal request(s)", removed)
        except Exception as e:
            logger.warning("Onboarding request retention sweep failed: %s", e)

    async def _find_stuck_downstream(self, blocked_task_id: str) -> list[Task]:
        """BFS walk of the dependency graph to find orphaned DEFINED tasks.

        Starting from a BLOCKED task, walks forward through ``get_dependents``
        and collects every downstream task still in DEFINED status.  These
        tasks can never proceed because their dependency chain is broken.

        Only DEFINED tasks are collected — tasks that have already been
        promoted past the dependency gate (READY, IN_PROGRESS, etc.) are
        not affected by the upstream blockage.

        Used by ``_notify_stuck_chain`` to give operators visibility into
        the full blast radius when a task fails or is stopped.
        """
        stuck: list[Task] = []
        visited: set[str] = set()
        queue: list[str] = [blocked_task_id]

        while queue:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            dependents = await self.db.get_dependents(current_id)
            for dep_id in dependents:
                if dep_id in visited:
                    continue
                task = await self.db.get_task(dep_id)
                if not task:
                    continue
                # Only DEFINED tasks are "stuck" — tasks in other states
                # (READY, IN_PROGRESS, etc.) have already moved past the
                # dependency gate.
                if task.status == TaskStatus.DEFINED:
                    stuck.append(task)
                    # Continue walking: this stuck task may itself have
                    # downstream dependents.
                    queue.append(dep_id)

        return stuck

    async def _notify_stuck_chain(self, blocked_task: Task) -> None:
        """Check for downstream stuck tasks and send a notification.

        Uses ``_find_stuck_downstream`` to do a BFS walk of the dependency
        graph.  If any DEFINED tasks are found that are transitively blocked
        by this task, sends a single consolidated notification listing all
        affected downstream tasks so operators can decide whether to skip,
        retry, or manually unblock the chain.
        """
        stuck = await self._find_stuck_downstream(blocked_task.id)
        if not stuck:
            return

        await self._emit_notify(
            "notify.chain_stuck",
            ChainStuckEvent(
                blocked_task=build_task_detail(blocked_task),
                stuck_task_ids=[t.id for t in stuck],
                stuck_task_titles=[t.title for t in stuck],
                project_id=blocked_task.project_id,
            ),
        )
        await self.db.log_event(
            "chain_stuck",
            project_id=blocked_task.project_id,
            task_id=blocked_task.id,
            payload=f"stuck_count={len(stuck)}, stuck_ids={[t.id for t in stuck[:20]]}",
        )

    # Budget warning thresholds — notify once per threshold crossing.
    #
    # IMPORTANT: This class attribute and the ``_check_budget_warning`` method
    # below intentionally SHADOW the earlier definitions (``_BUDGET_WARNING_THRESHOLDS``
    # and the first ``_check_budget_warning`` at line ~469).  Python resolves
    # method lookups top-down within the class body, so the LAST definition
    # wins at runtime.  This version uses cumulative token usage (simpler)
    # instead of rolling-window-scoped usage.
    #
    # TODO: consolidate the two implementations into one.  The shadowed version
    # (earlier in this file) is dead code at runtime.
    _BUDGET_THRESHOLDS: list[int] = [80, 95]

    async def _check_budget_warning(
        self,
        project_id: str,
        tokens_added: int,
    ) -> None:
        """Send a budget warning if a project crosses a spending threshold.

        Called after recording token usage for a completed task.  Queries
        the project's cumulative token usage and ``budget_limit``, then
        checks whether usage has crossed any of the ``_BUDGET_THRESHOLDS``
        percentage levels.  Each threshold (80%, 95%) fires at most once
        per project; the ``_budget_warned_at`` dict tracks the highest
        threshold already notified to avoid duplicate alerts.

        Note: this method shadows an earlier definition that uses rolling-
        window scoped usage.  The shadowed version is unreachable at runtime.
        """
        project = await self.db.get_project(project_id)
        if not project or project.budget_limit is None or project.budget_limit <= 0:
            return

        usage = await self.db.get_project_token_usage(project_id)
        pct = usage / project.budget_limit * 100

        prev_threshold = self._budget_warned_at.get(project_id, 0)

        for threshold in self._BUDGET_THRESHOLDS:
            if pct >= threshold > prev_threshold:
                await self._emit_notify(
                    "notify.budget_warning",
                    BudgetWarningEvent(
                        project_name=project.name,
                        usage=usage,
                        limit=project.budget_limit,
                        percentage=pct,
                        project_id=project_id,
                    ),
                )
                await self.db.log_event(
                    "budget_warning",
                    project_id=project_id,
                    payload=f"threshold={threshold}%, usage={usage:,}/{project.budget_limit:,}",
                )
                self._budget_warned_at[project_id] = threshold
