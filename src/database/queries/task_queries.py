"""Task CRUD and filtering operations."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import delete, insert, literal, null, select, update, func, and_

from src.database.tables import (
    events,
    archived_tasks,
    gates,
    sessions,
    task_context,
    task_comments,
    task_criteria,
    task_dependencies,
    task_gates,
    task_labels,
    task_metadata,
    task_results,
    task_tools,
    task_workspace_requirements,
    tasks,
    workspaces,
)
from src.database.queries.blocked_state import (
    PROJECTION_INPUT_COLUMNS,
    apply_label_filters,
)
from src.models import (
    HOLD_LABEL_PREFIX,
    Task,
    TaskStatus,
    TaskType,
    VerificationType,
    WorkspaceMode,
)
from src.state_machine import is_valid_status_transition

logger = logging.getLogger(__name__)


#: ``UPDATE … RETURNING`` / ``INSERT … RETURNING`` landed in SQLite 3.35.
#: PostgreSQL has always had it.  Every RETURNING-based fast path in the
#: claim path (spec §15) keeps a two-statement fallback for older SQLite.
SQLITE_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)


def supports_returning(conn) -> bool:
    """True when *conn*'s dialect can run ``… RETURNING`` (see above)."""
    return conn.dialect.name != "sqlite" or SQLITE_RETURNING


#: Statuses that no clause of ``blocked_predicate()`` can distinguish: the
#: predicate reads only COMPLETED (``blocks`` / ``waits-for``), DEFINED and
#: AWAITING_PLAN_APPROVAL (``parent-child``) and BLOCKED / terminal FAILED
#: (``conditional-blocks``).  A move *between* two of these statuses can
#: therefore never change any task's ``is_blocked`` — which is exactly the
#: assertion ``projection_stable=True`` makes (READY→IN_PROGRESS on claim,
#: IN_PROGRESS→READY on release).
_PROJECTION_NEUTRAL_STATUSES = frozenset({TaskStatus.READY, TaskStatus.IN_PROGRESS})


#: Maps ``transition_task``'s ``context`` to the ``task.ready`` audit reason
#: (spec §9). Every context starting with ``session_`` maps to "released"
#: (see ``_ready_reason`` below — covers ``session_exited_without_close``,
#: ``session_launch_failed``, ``session_rapid_crash``,
#: ``session_stalled_restart``, ``session_not_live``, ``session_close`` and
#: any future ``session_*`` context without needing a new entry here);
#: unknown non-``session_`` contexts default to "promoted".
_READY_REASONS = {
    "promotion": "promoted",
    "reopen_with_feedback": "restarted",
    "retry": "released",
    "rate_limit": "resumed",
    "resume_paused": "resumed",
    "slot_reset_failed": "released",
    "prepare_timeout": "released",
}


def _ready_reason(context: str) -> str:
    """Resolve a ``transition_task`` context to its ``task.ready`` reason."""
    if context in _READY_REASONS:
        return _READY_REASONS[context]
    if context.startswith("session_"):
        return "released"
    return "promoted"


class StaleClaim(Exception):
    """A fenced write found the task held under a different claim epoch (spec §10)."""


@dataclass
class TransitionResult:
    """What one status write changed besides the row itself."""

    flipped: set[str] = field(default_factory=set)
    settled: list[str] = field(default_factory=list)
    #: Descendant ids closed as ``abandoned`` — populated only by
    #: ``HierarchyQueryMixin.abandon_subtree`` (spec §7); empty otherwise.
    abandoned: list[str] = field(default_factory=list)
    #: ``(task_id, reason)`` for every task that entered the ready frontier
    #: in this transaction (spec §9).
    ready: list[tuple[str, str]] = field(default_factory=list)
    #: The task row as it stands after the write — populated only when the
    #: caller passed ``returning=True`` to ``_apply_transition`` (spec §15's
    #: claim/release fast paths, which build their ``Task`` from it instead
    #: of re-reading).  ``None`` also means "the guarded UPDATE matched no
    #: row" for callers that passed ``extra_where``.
    row: dict | None = None


class TaskQueryMixin:
    """Query mixin for task operations.  Expects ``self._engine``."""

    async def create_task(
        self, task: Task, *, conn=None, routing_policy: Callable[[Task], bool] | None = None,
    ) -> None:
        """Insert a task and its configured routing gate in the same transaction."""
        async def write(connection):
            await self._insert_task_row(task, conn=connection)
            gated = routing_policy is not None and routing_policy(task)
            if gated:
                await self.create_gate(
                    task.project_id, "routing", "Route task",
                    question="Assign profile + intelligence class (+ workspace if profile needs one).",
                    waiter_task_ids=[task.id], conn=connection,
                )
                task.is_blocked = True
            return gated
        if conn is not None:
            await write(conn)
            return
        async with self._engine.begin() as conn:
            gated = await write(conn)
        if gated:
            await self.log_blocked_flips({task.id})

    async def _insert_task_row(self, task: Task, *, conn) -> None:
        """Insert a single task row.  Caller owns the transaction."""
        archived_project = (await conn.execute(
            select(archived_tasks.c.project_id).where(archived_tasks.c.id == task.id).with_for_update()
        )).scalar_one_or_none()
        if archived_project is not None and archived_project != task.project_id:
            raise ValueError("Cannot recreate an archived task in another project")
        now = time.time()
        await conn.execute(
            insert(tasks).values(
                id=task.id,
                project_id=task.project_id,
                parent_task_id=task.parent_task_id,
                repo_id=task.repo_id,
                title=task.title,
                description=task.description,
                priority=task.priority,
                status=task.status.value,
                verification_type=task.verification_type.value,
                retry_count=task.retry_count,
                max_retries=task.max_retries,
                assigned_agent_id=task.assigned_agent_id,
                branch_name=task.branch_name,
                resume_after=task.resume_after,
                requires_approval=int(task.requires_approval),
                pr_url=task.pr_url,
                plan_source=task.plan_source,
                is_plan_subtask=int(task.is_plan_subtask),
                task_type=task.task_type.value if task.task_type else None,
                profile_id=task.profile_id,
                preferred_workspace_id=task.preferred_workspace_id,
                attachments=json.dumps(task.attachments) if task.attachments else "[]",
                auto_approve_plan=int(task.auto_approve_plan),
                skip_verification=int(task.skip_verification),
                workflow_id=task.workflow_id,
                affinity_agent_id=task.affinity_agent_id,
                affinity_reason=task.affinity_reason,
                workspace_mode=(task.workspace_mode.value if task.workspace_mode else None),
                dedup_key=task.dedup_key,
                discord_thread_id=task.discord_thread_id,
                intelligence_class=task.intelligence_class,
                created_by_kind=task.created_by_kind,
                created_by_id=task.created_by_id,
                # A brand-new row has no edges yet, so it starts
                # unblocked; the edges that follow recompute it
                # (work-graph implementation spec §4.1).
                is_blocked=0,
                created_at=now,
                updated_at=now,
            )
        )

    async def get_task(self, task_id: str) -> Task | None:
        """Fetch a single task by ID."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(tasks).where(tasks.c.id == task_id))
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_task(row)

    async def _get_task_conn(self, task_id: str, *, conn) -> Task | None:
        """Fetch a single task by ID on a caller-supplied connection."""
        result = await conn.execute(select(tasks).where(tasks.c.id == task_id))
        row = result.mappings().fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    async def list_tasks(
        self,
        project_id: str | None = None,
        status: TaskStatus | None = None,
        *,
        labels: list[str] | None = None,
        any_label: list[str] | None = None,
    ) -> list[Task]:
        """List tasks with optional project/status/label filters.

        ``labels`` is all-of, ``any_label`` is any-of (work-graph design §6).
        Neither filters ``hold:*`` — listing shows what *exists*; only the
        ready frontier filters what to *do*.
        """
        stmt = select(tasks)
        conditions = []
        if project_id:
            conditions.append(tasks.c.project_id == project_id)
        if status:
            conditions.append(tasks.c.status == status.value)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        if labels or any_label:
            stmt = apply_label_filters(stmt, labels=labels, any_label=any_label)
        stmt = stmt.order_by(tasks.c.priority.asc(), tasks.c.created_at.asc())
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def list_active_tasks(
        self,
        project_id: str | None = None,
        exclude_statuses: set[TaskStatus] | None = None,
    ) -> list[Task]:
        """List non-terminal tasks, optionally filtered by project."""
        if exclude_statuses is None:
            exclude_statuses = {TaskStatus.COMPLETED}

        conditions = []
        if exclude_statuses:
            conditions.append(tasks.c.status.notin_([s.value for s in exclude_statuses]))
        if project_id:
            conditions.append(tasks.c.project_id == project_id)

        stmt = select(tasks)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(tasks.c.priority.asc(), tasks.c.created_at.asc())
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def list_active_tasks_all_projects(self) -> list[Task]:
        """Return all non-completed tasks across every project."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(tasks)
                .where(tasks.c.status != TaskStatus.COMPLETED.value)
                .order_by(
                    tasks.c.project_id.asc(),
                    tasks.c.priority.asc(),
                    tasks.c.created_at.asc(),
                )
            )
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def count_tasks_by_status(
        self,
        project_id: str | None = None,
    ) -> dict[str, int]:
        """Return a {status_value: count} mapping for quick summary stats."""
        stmt = select(tasks.c.status, func.count().label("cnt"))
        if project_id:
            stmt = stmt.where(tasks.c.project_id == project_id)
        stmt = stmt.group_by(tasks.c.status)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return {r["status"]: r["cnt"] for r in result.mappings().fetchall()}

    @staticmethod
    def _coerce_task_values(kwargs: dict) -> dict:
        """Normalise enum-valued kwargs to their DB representation."""
        values = {}
        for key, value in kwargs.items():
            if isinstance(value, (TaskStatus, VerificationType, TaskType, WorkspaceMode)):
                value = value.value
            values[key] = value
        return values

    async def update_task(self, task_id: str, **kwargs) -> None:
        """Update arbitrary task fields.

        ``status`` should be routed through :meth:`transition_task`, which
        validates the move and recomputes the blocked-state projection.  A
        raw ``status=`` here still recomputes (so the projection can never
        go stale) but skips validation; an invariant test guards production
        call sites.

        The recompute fires for **any** ``PROJECTION_INPUT_COLUMNS`` write,
        not just ``status``: ``update_task(primary, max_retries=10)`` turns a
        terminal failure back into a transient one and must re-block the
        contingency waiting on it.
        """
        values = self._coerce_task_values(kwargs)
        values["updated_at"] = time.time()
        async with self._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))
            flipped: set[str] = set()
            if PROJECTION_INPUT_COLUMNS & kwargs.keys():
                flipped = await self.recompute_blocked({task_id}, conn=conn)
        await self.log_blocked_flips(flipped)

    def set_state_machine_enforcement(self, enforce: bool) -> None:
        """Toggle state-machine enforcement in ``transition_task``.

        Called from config load/reload so the query layer doesn't have to
        import ``AppConfig``.  Default is warn-only (``False``).  See
        work-graph §9 stage 4 for the rollout.
        """
        self._sm_enforce = bool(enforce)

    async def _note_frontier_entry(self, conn, task_ids: set[str], *, reason: str) -> list[str]:
        """Record every id in *task_ids* that is now in the ready frontier (spec §9).

        Callers pass ids whose pre-state was outside the frontier; this checks
        the post-state in one statement and writes the ``task.ready`` audit row
        on the caller's connection so a crash after commit cannot lose it.

        Where the dialect supports it (SQLite >= 3.35, always on PostgreSQL)
        the check and the audit insert are the *same* statement — an
        ``INSERT … SELECT … RETURNING task_id`` — which is what keeps the
        release path inside its spec §15 budget.  The two-statement form is
        kept verbatim for older SQLite.
        """
        if not task_ids:
            return []

        frontier = and_(
            tasks.c.id.in_(sorted(task_ids)),
            tasks.c.status == TaskStatus.READY.value,
            tasks.c.is_blocked == 0,
        )
        if supports_returning(conn):
            src = apply_label_filters(
                select(
                    literal("task.ready"),
                    tasks.c.project_id,
                    tasks.c.id,
                    # ``agent_id`` — a genuine SQL NULL.  ``literal(None)``
                    # renders as an untyped bind parameter, which some
                    # backends reject in an ``INSERT … SELECT``.
                    null(),
                    literal(reason),
                    literal(time.time()),
                ).where(frontier),
                exclude_hold=True,
            )
            stmt = (
                insert(events)
                .from_select(
                    ["event_type", "project_id", "task_id", "agent_id", "payload", "timestamp"],
                    src,
                )
                .returning(events.c.task_id)
            )
            return [r[0] for r in (await conn.execute(stmt)).fetchall()]

        stmt = apply_label_filters(
            select(tasks.c.id, tasks.c.project_id, tasks.c.title).where(frontier),
            exclude_hold=True,
        )
        rows = (await conn.execute(stmt)).fetchall()
        for tid, pid, _title in rows:
            await self.log_event(
                "task.ready", project_id=pid, task_id=tid, payload=reason, conn=conn
            )
        return [r[0] for r in rows]

    async def _apply_transition(
        self,
        conn,
        task_id: str,
        new_status: TaskStatus,
        *,
        context: str = "",
        event=None,
        force: bool = False,
        _settle_depth: int = 0,
        expect_claim_epoch: int | None = None,
        projection_stable: bool = False,
        extra_where=None,
        extra_values: dict | None = None,
        returning: bool = False,
        assume_pre_state: tuple[TaskStatus, bool] | None = None,
        **kwargs,
    ) -> TransitionResult:
        """Update task status with state-machine validation, on a caller-owned connection.

        Read, validate, apply and recompute ``is_blocked`` happen in **one**
        transaction (work-graph implementation spec §4.1), so no reader can
        observe the new status against the old projection.

        Validation stays warn-only in this phase — the ``state_machine.
        enforce`` flag and the ``force`` bypass land with WG-5.

        Returns the set of task ids whose ``is_blocked`` flipped — including
        any flips caused by settling a container this write completed — plus
        any containers settled by this write (spec §7); the matching
        ``task.blocked`` / ``task.unblocked`` audit rows and the settlement
        listener callback are the caller's job, after the transaction commits.

        ``_settle_depth`` is private: it is only ever passed by
        ``settle_containers`` recursing into its own parent one level
        deeper, and is never a real task column, so it must stay a named
        keyword rather than fall into ``**kwargs`` (which feeds
        ``_coerce_task_values``).

        A same-status call is **not** a no-op for the projection: it can still
        carry a ``PROJECTION_INPUT_COLUMNS`` write (a FAILED task bumped to
        ``retry_count == max_retries`` turns a transient failure terminal,
        satisfying every ``conditional-blocks`` edge pointing at it), so it
        recomputes too.

        The remaining keywords are the spec §15 claim-path fast paths.  All
        are additive; every pre-existing caller keeps the behaviour above.

        ``projection_stable=True`` is the caller's **assertion** that this
        write cannot change any task's ``is_blocked``.  It is honoured only
        when both the pre- and post-status are in
        ``_PROJECTION_NEUTRAL_STATUSES`` (READY / IN_PROGRESS) — every other
        move, terminal ones included, silently falls back to the full
        recompute, so a mistaken assertion cannot corrupt the projection.
        When honoured, ``recompute_blocked`` and the *dependents'* frontier
        bookkeeping are skipped; the task's **own** frontier entry is still
        recorded when its post-state is READY and unblocked (that is the
        release path's ``task.ready`` / ``released`` audit row).

        ``extra_where`` / ``extra_values`` fold a caller's guard and extra
        columns into the single status UPDATE (the claim's epoch bump and
        its ``status = READY AND is_blocked = 0 AND assigned_agent_id IS
        NULL`` fence), so the write still goes through this one sanctioned
        path.  With ``returning=True`` the updated row comes back in
        ``TransitionResult.row`` (``None`` when the guard matched nothing)
        and the caller builds its ``Task`` from it instead of re-reading.

        ``assume_pre_state`` is ``(status, is_blocked)`` asserted by the
        caller *in the same statement* via ``extra_where``; it skips the
        pre-read.  Only pass it when ``extra_where`` pins both values, so
        that a matched UPDATE proves the assertion.
        """
        values = self._coerce_task_values(kwargs)
        result = TransitionResult()

        if assume_pre_state is not None:
            current_status, pre_blocked = assume_pre_state
        else:
            row = (
                await conn.execute(
                    select(tasks.c.status, tasks.c.is_blocked).where(tasks.c.id == task_id)
                )
            ).fetchone()

            if row is None:
                logger.warning("transition_task: task '%s' not found, cannot validate", task_id)
                values["status"] = new_status.value
                values["updated_at"] = time.time()
                await conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))
                return TransitionResult()

            current_status = TaskStatus(row[0])
            pre_blocked = bool(row[1])

        was_frontier = current_status == TaskStatus.READY and not pre_blocked
        stable = (
            projection_stable
            and current_status in _PROJECTION_NEUTRAL_STATUSES
            and new_status in _PROJECTION_NEUTRAL_STATUSES
        )

        async def _write(values: dict):
            """Run the guarded UPDATE; return ``(matched, row_or_None)``."""
            stmt = update(tasks).where(tasks.c.id == task_id)
            if expect_claim_epoch is not None:
                stmt = stmt.where(
                    and_(
                        tasks.c.claim_epoch == expect_claim_epoch,
                        tasks.c.assigned_agent_id.isnot(None),
                    )
                )
            if extra_where is not None:
                stmt = stmt.where(extra_where)
            if extra_values:
                values = {**values, **extra_values}
            stmt = stmt.values(**values)
            if returning and supports_returning(conn):
                out = (await conn.execute(stmt.returning(*tasks.c))).mappings().fetchone()
                return out is not None, (dict(out) if out is not None else None)
            res = await conn.execute(stmt)
            if res.rowcount == 0:
                return False, None
            if not returning:
                return True, None
            out = (
                (await conn.execute(select(tasks).where(tasks.c.id == task_id)))
                .mappings()
                .fetchone()
            )
            return True, (dict(out) if out is not None else None)

        if current_status == new_status:
            if values or extra_values:
                values["updated_at"] = time.time()
                matched, result.row = await _write(values)
                if not matched and expect_claim_epoch is not None:
                    raise StaleClaim(f"{task_id}: claim epoch {expect_claim_epoch} is not current")
                if not matched:
                    return result
                if not stable and PROJECTION_INPUT_COLUMNS & values.keys():
                    result.flipped = await self.recompute_blocked({task_id}, conn=conn)
                    # A same-status write can still flip is_blocked (e.g. a
                    # FAILED task's retry_count reaching max_retries turns a
                    # transient failure terminal — see the docstring above).
                    # ``was_frontier`` already keeps a plain READY->READY
                    # write silent: the task itself only shows up in
                    # ``result.flipped`` here if its own ``is_blocked``
                    # actually changed, so this single call covers both the
                    # task entering the frontier and any dependent it
                    # unblocks.
                    for tid in await self._note_frontier_entry(
                        conn, set(result.flipped), reason="unblocked"
                    ):
                        result.ready.append((tid, "unblocked"))
        else:
            # Invariant 6 (spec §7): a container never reaches COMPLETED while
            # a child is still open.  Enforced HERE rather than only at the
            # close surfaces, because approval, execution and the workflow
            # sync all complete tasks without passing through them.
            # ``force=True`` (abandonment, administrative closes) bypasses it.
            if new_status == TaskStatus.COMPLETED and not force:
                from src.database.queries.hierarchy_queries import HierarchyError

                open_ids = [
                    r[0]
                    for r in (
                        await conn.execute(
                            select(tasks.c.id)
                            .where(
                                and_(
                                    tasks.c.parent_task_id == task_id,
                                    tasks.c.status.notin_(
                                        (
                                            TaskStatus.COMPLETED.value,
                                            TaskStatus.FAILED.value,
                                        )
                                    ),
                                )
                            )
                            .order_by(tasks.c.id)
                            .limit(10)
                        )
                    ).fetchall()
                ]
                if open_ids:
                    raise HierarchyError(
                        "open_children",
                        f"{task_id} has open child(ren): {', '.join(open_ids)}",
                    )

            if not is_valid_status_transition(current_status, new_status):
                ctx = f" ({context})" if context else ""
                # WG-5: enforce raises when the flag is on and the
                # caller didn't opt out via ``force=True``.  Warn-only
                # otherwise (unchanged pre-flip behaviour).
                if getattr(self, "_sm_enforce", False) and not force:
                    from src.state_machine import InvalidTransition

                    raise InvalidTransition(
                        current_status,
                        event,
                        from_status=current_status,
                        to_status=new_status,
                    )
                logger.warning(
                    "Invalid task status transition: %s -> %s for task '%s'%s",
                    current_status.value,
                    new_status.value,
                    task_id,
                    ctx,
                )

            values["status"] = new_status.value
            values["updated_at"] = time.time()
            matched, result.row = await _write(values)
            if not matched and expect_claim_epoch is not None:
                raise StaleClaim(f"{task_id}: claim epoch {expect_claim_epoch} is not current")
            if not matched:
                # A caller-supplied ``extra_where`` guard lost its race (the
                # claim fence).  Nothing was written, so there is nothing to
                # project or announce.
                return result
            if not stable:
                result.flipped = await self.recompute_blocked({task_id}, conn=conn)

            if not was_frontier:
                reason = _ready_reason(context)
                for tid in await self._note_frontier_entry(conn, {task_id}, reason=reason):
                    result.ready.append((tid, reason))

            for tid in await self._note_frontier_entry(
                conn, {t for t in result.flipped if t != task_id}, reason="unblocked"
            ):
                result.ready.append((tid, "unblocked"))

            # A task that will not run again cannot satisfy a gate by
            # waiting, so retire any gate now left with only terminal
            # waiters. In the same transaction as the status write: a
            # reader must never see a finished task still gating work.
            if new_status.value in self._TERMINAL_TASK_STATUSES:
                await self.expire_satisfied_gates(task_id, conn=conn)

            if new_status == TaskStatus.COMPLETED:
                parent = (
                    await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id == task_id))
                ).scalar()
                if parent:
                    settle_result = await self.settle_containers(
                        {parent}, conn=conn, depth=_settle_depth + 1
                    )
                    result.settled.extend(settle_result.settled)
                    result.flipped |= settle_result.flipped
                    result.ready.extend(settle_result.ready)

        return result

    def set_settlement_listener(self, cb) -> None:
        """Register the post-commit callback for settled containers (spec §7)."""
        self._settlement_listener = cb

    async def _notify_settled(self, settled: list[str]) -> None:
        """Fire the settlement listener, if any, with the ids settled by one write.

        Called from :meth:`transition_task` below, and also directly by
        ``DependencyQueryMixin`` and ``HierarchyQueryMixin`` (``add_dependency``,
        ``remove_dependency``) via the composed database adapter, since those
        mixins reach ``set_parent`` without going through ``transition_task``.
        """
        if settled and self._settlement_listener is not None:
            try:
                await self._settlement_listener(list(settled))
            except Exception:  # a listener failure must not fail the transition
                logger.exception("settlement listener failed for %s", settled)

    def set_ready_listener(self, cb) -> None:
        """Register the post-commit callback for ready-frontier entries (spec §9)."""
        self._ready_listener = cb

    async def _notify_ready(self, entries: list[tuple[str, str]]) -> None:
        """Fire the ready listener, if any, with the ``(task_id, reason)`` pairs.

        Called from :meth:`transition_task` below, and also directly by
        ``DependencyQueryMixin`` (``add_dependency``, ``remove_dependency``)
        and ``GateQueryMixin.resolve_gate``, which reach the frontier without
        going through :meth:`_apply_transition`.
        """
        if entries and self._ready_listener is not None:
            try:
                await self._ready_listener(list(entries))
            except Exception:  # a listener failure must not fail the transition
                logger.exception("ready listener failed for %s", entries)

    async def transition_task(
        self,
        task_id: str,
        new_status: TaskStatus,
        *,
        context: str = "",
        event=None,
        force: bool = False,
        expect_claim_epoch: int | None = None,
        **kwargs,
    ) -> set[str]:
        """Public status write: one transaction, then post-commit emission.

        Returns the blocked-state flips (unchanged contract).  Settled
        containers are delivered to the settlement listener after commit.
        """
        async with self._engine.begin() as conn:
            result = await self._apply_transition(
                conn,
                task_id,
                new_status,
                context=context,
                event=event,
                force=force,
                expect_claim_epoch=expect_claim_epoch,
                **kwargs,
            )
        await self.log_blocked_flips(result.flipped)
        await self._notify_settled(result.settled)
        await self._notify_ready(result.ready)
        return result.flipped

    #: Statuses after which a task will not run again, so anything gated on
    #: it can never be satisfied by waiting.
    _TERMINAL_TASK_STATUSES = ("COMPLETED", "FAILED", "CANCELLED")

    #: Post-commit settlement callback, registered via ``set_settlement_listener``.
    _settlement_listener = None

    #: Post-commit ready-frontier callback, registered via ``set_ready_listener``.
    _ready_listener = None

    async def expire_satisfied_gates(self, task_id: str, *, conn=None) -> int:
        """Expire open gates whose every waiter has reached a terminal state.

        A routing gate is attached to each new task and resolved by the triage
        agent. When the task finishes without that happening — routed by hand,
        completed by an agent that never consulted the gate, closed as junk —
        the gate stays ``open`` forever with a waiter that will never run
        again. They accumulate silently: three survived a full queue cleanup
        here, each pinned to a COMPLETED task.

        Scoped to ``routing`` gates on purpose. Every other type has its own
        resolution path — a ``task`` gate resolves when the task it awaits
        finishes, ``pr-merged`` on the merge, ``human`` on a click — and those
        can legitimately outlive their waiters; resolving is a more meaningful
        outcome than expiring, and pre-empting it loses that signal. A routing
        gate is different: it is resolved *by* the triage agent acting on the
        waiter, so once every waiter is terminal nothing can ever resolve it.

        Only gates whose waiters are *all* terminal are expired; a gate shared
        with a live task is left alone. Returns how many were expired.
        """
        own = conn is None
        if own:
            ctx = self._engine.begin()
            conn = await ctx.__aenter__()
        try:
            gate_ids = [
                r[0]
                for r in (
                    await conn.execute(
                        select(task_gates.c.gate_id).where(task_gates.c.task_id == task_id)
                    )
                ).fetchall()
            ]
            expired = 0
            for gate_id in gate_ids:
                live = (
                    await conn.execute(
                        select(task_gates.c.task_id)
                        .select_from(task_gates.join(tasks, tasks.c.id == task_gates.c.task_id))
                        .where(
                            and_(
                                task_gates.c.gate_id == gate_id,
                                tasks.c.status.notin_(self._TERMINAL_TASK_STATUSES),
                            )
                        )
                        .limit(1)
                    )
                ).fetchone()
                if live is not None:
                    continue
                result = await conn.execute(
                    update(gates)
                    .where(
                        and_(
                            gates.c.id == gate_id,
                            gates.c.status == "open",
                            gates.c.gate_type == "routing",
                        )
                    )
                    .values(status="expired", resolution="all waiters terminal")
                )
                expired += result.rowcount or 0
            return expired
        finally:
            if own:
                await ctx.__aexit__(None, None, None)

    async def delete_task(
        self, task_id: str, *, cascade: bool = False, conn=None
    ) -> TransitionResult:
        """Delete a task; with *cascade*, its whole subtree (spec §7).

        Refuses a container with children unless *cascade*.  One transaction:
        dependents are snapshotted while the edges exist, the subtree is
        removed deepest-first, the former container is settled, and the
        projection is recomputed.

        When *conn* is supplied, the caller already owns the transaction
        (e.g. to check ``live_descendant_sessions`` atomically with the
        delete) — this method writes on it and returns the accumulated
        ``TransitionResult`` instead of opening its own transaction and
        firing the post-commit notifications itself; the caller is then
        responsible for ``log_blocked_flips`` / ``_notify_settled`` /
        ``_notify_ready`` once its own transaction has committed.
        """
        if conn is not None:
            return await self._delete_task_body(task_id, cascade=cascade, conn=conn)

        async with self._engine.begin() as c:
            result = await self._delete_task_body(task_id, cascade=cascade, conn=c)
        await self.log_blocked_flips(result.flipped)
        await self._notify_settled(result.settled)
        await self._notify_ready(result.ready)
        return result

    async def _delete_task_body(self, task_id: str, *, cascade: bool, conn) -> TransitionResult:
        """The transactional body of :meth:`delete_task`, on a supplied ``conn``."""
        from src.database.queries.hierarchy_queries import HierarchyError

        parent = (
            await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id == task_id))
        ).scalar()
        ids = await self.subtree_ids(task_id, conn=conn)
        if len(ids) > 1 and not cascade:
            raise HierarchyError("has_children", f"{task_id} has {len(ids) - 1} descendant(s)")
        affected = await self._collect_affected(set(ids), conn)
        affected -= set(ids)
        if parent:
            affected.add(parent)
        for tid in reversed(ids):  # deepest first (subtree_ids is shallow→deep)
            await self._delete_one(tid, conn=conn)
        flipped = await self.recompute_blocked(affected, conn=conn) if affected else set()
        # Deleting a blocker unblocks its dependents exactly as completing it
        # would, so the same ``task.ready`` audit row and listener wake-up are
        # owed — without them a waiting ``task_claim`` long-poll sleeps
        # through work that just became claimable.
        ready = [
            (tid, "unblocked")
            for tid in await self._note_frontier_entry(conn, flipped, reason="unblocked")
        ]
        settle_result = await self.settle_containers({parent} if parent else set(), conn=conn)
        return TransitionResult(
            flipped=flipped | settle_result.flipped,
            settled=settle_result.settled,
            ready=ready + list(settle_result.ready),
        )

    async def _delete_one(
        self,
        task_id: str,
        *,
        conn,
        preserve_comments: bool = False,
        gate_resolution: str = "last waiter task deleted",
    ) -> None:
        """Delete an active task and its FK references; archives retain comments.

        Gates the task was waiting on are unhooked here too, and any gate
        left with no waiters at all is marked ``expired``. Without this the
        ``task_gates`` FK (``NO ACTION``) simply refuses the delete, and
        force-removing those rows instead strands the gate ``open`` forever
        — which for a human gate means live Approve/Deny buttons in Discord
        for a task that no longer exists.
        """
        await conn.execute(delete(task_results).where(task_results.c.task_id == task_id))
        # token_ledger rows survive task deletion: the tokens were really
        # spent against the project's budget, so dropping them would
        # understate cost.  `delete_project` remains the bulk escape hatch
        # for actually purging a project's ledger.
        await conn.execute(
            delete(task_dependencies).where(
                (task_dependencies.c.task_id == task_id)
                | (task_dependencies.c.depends_on_task_id == task_id)
            )
        )
        await conn.execute(delete(task_criteria).where(task_criteria.c.task_id == task_id))
        await conn.execute(delete(task_context).where(task_context.c.task_id == task_id))
        await conn.execute(delete(task_metadata).where(task_metadata.c.task_id == task_id))
        await conn.execute(delete(task_labels).where(task_labels.c.task_id == task_id))
        await conn.execute(delete(task_tools).where(task_tools.c.task_id == task_id))

        # Gates: drop this task's waiter rows, then expire any gate that
        # is left with nothing waiting on it.  Collect the candidates
        # first — after the delete there is no way back to them.
        gate_ids = {
            r[0]
            for r in (
                await conn.execute(
                    select(task_gates.c.gate_id).where(task_gates.c.task_id == task_id)
                )
            ).fetchall()
        }
        await conn.execute(delete(task_gates).where(task_gates.c.task_id == task_id))
        for gate_id in gate_ids:
            still_waiting = (
                await conn.execute(
                    select(task_gates.c.task_id).where(task_gates.c.gate_id == gate_id).limit(1)
                )
            ).fetchone()
            if still_waiting is None:
                await conn.execute(
                    update(gates)
                    .where(and_(gates.c.id == gate_id, gates.c.status == "open"))
                    .values(status="expired", resolution=gate_resolution)
                )

        # Remaining FK holders. Without these the delete fails outright
        # with a ForeignKeyViolationError naming one table at a time, so
        # each is only discovered by hitting it.
        #
        # ``sessions`` is *nulled*, not deleted: a session row is the
        # historical record of an agent run — how long it lived, what it
        # cost — and that stays true after the task is gone. The other two
        # describe the task's claim on resources and mean nothing without
        # it, so the requirement rows go and the workspace lock is
        # released rather than left pointing at a task that no longer
        # exists.
        await conn.execute(
            update(sessions).where(sessions.c.task_id == task_id).values(task_id=None)
        )
        await conn.execute(
            delete(task_workspace_requirements).where(
                task_workspace_requirements.c.task_id == task_id
            )
        )
        await conn.execute(
            update(workspaces)
            .where(workspaces.c.locked_by_task_id == task_id)
            .values(locked_by_task_id=None, locked_by_agent_id=None, locked_at=None)
        )

        await conn.execute(delete(tasks).where(tasks.c.id == task_id))
        # The parent DELETE waits for an accepted comment's UPDATE lock.
        # Cleanup afterwards cannot miss a concurrently committed append.
        if not preserve_comments:
            await conn.execute(delete(task_comments).where(task_comments.c.task_id == task_id))

    async def get_task_updated_at(self, task_id: str) -> float | None:
        """Return the ``updated_at`` timestamp for a task, or *None*."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(tasks.c.updated_at).where(tasks.c.id == task_id))
            row = result.fetchone()
            return row[0] if row else None

    async def get_task_created_at(self, task_id: str) -> float | None:
        """Return the ``created_at`` timestamp for a task, or *None*."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(tasks.c.created_at).where(tasks.c.id == task_id))
            row = result.fetchone()
            return row[0] if row else None

    async def add_task_context(
        self,
        task_id: str,
        *,
        type: str,
        label: str,
        content: str,
    ) -> str:
        """Insert a task_context row and return its generated ID."""
        ctx_id = str(uuid.uuid4())[:12]
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(task_context).values(
                    id=ctx_id,
                    task_id=task_id,
                    type=type,
                    label=label,
                    content=content,
                )
            )
        return ctx_id

    async def get_task_contexts(self, task_id: str) -> list[dict]:
        """Return all task_context rows for *task_id* as dicts."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(
                    task_context.c.id,
                    task_context.c.task_id,
                    task_context.c.type,
                    task_context.c.label,
                    task_context.c.content,
                ).where(task_context.c.task_id == task_id)
            )
            return [dict(r) for r in result.mappings().fetchall()]

    # ---- task_metadata (key-value store) ----

    async def set_task_meta(self, task_id: str, key: str, value) -> None:
        """Upsert a single metadata key for a task. *value* is JSON-serialised."""
        encoded = json.dumps(value)
        async with self._engine.begin() as conn:
            # Try update first; if no row matched, insert.
            result = await conn.execute(
                update(task_metadata)
                .where(
                    and_(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == key,
                    )
                )
                .values(value=encoded)
            )
            if result.rowcount == 0:
                await conn.execute(
                    insert(task_metadata).values(task_id=task_id, key=key, value=encoded)
                )

    async def get_task_meta(self, task_id: str, key: str):
        """Return a single metadata value (JSON-decoded), or ``None``."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_metadata.c.value).where(
                    and_(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == key,
                    )
                )
            )
            row = result.fetchone()
            return json.loads(row[0]) if row else None

    async def get_all_task_meta(self, task_id: str) -> dict:
        """Return all metadata for a task as ``{key: decoded_value}``."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_metadata.c.key, task_metadata.c.value).where(
                    task_metadata.c.task_id == task_id
                )
            )
            return {r.key: json.loads(r.value) for r in result.fetchall()}

    async def delete_task_meta(self, task_id: str, key: str) -> None:
        """Remove a single metadata key for a task."""
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(task_metadata).where(
                    and_(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == key,
                    )
                )
            )

    # ---- task_labels (free-text tags — aq-surface spec `task_set`) ----

    async def add_task_label(self, task_id: str, label: str) -> None:
        """Attach a label to a task. No-op if already present."""
        async with self._engine.begin() as conn:
            existing = await conn.execute(
                select(task_labels.c.label).where(
                    and_(task_labels.c.task_id == task_id, task_labels.c.label == label)
                )
            )
            if existing.fetchone() is None:
                await conn.execute(insert(task_labels).values(task_id=task_id, label=label))

    async def remove_task_label(self, task_id: str, label: str) -> list[str]:
        """Detach a label from a task. No-op if not present.

        Removing a ``hold:*`` label can expose the task to the ready
        frontier (design §6), so this records the frontier entry (reason
        ``hold_removed``) in the same transaction and returns the ids that
        entered — empty for a non-hold label or a task that isn't now in
        the frontier.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(task_labels).where(
                    and_(task_labels.c.task_id == task_id, task_labels.c.label == label)
                )
            )
            if not label.startswith(HOLD_LABEL_PREFIX):
                return []
            return await self._note_frontier_entry(conn, {task_id}, reason="hold_removed")

    async def get_task_labels(self, task_id: str) -> list[str]:
        """Return all labels attached to a task, sorted."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_labels.c.label).where(task_labels.c.task_id == task_id)
            )
            return sorted(r[0] for r in result.fetchall())

    async def get_group_progress(self, parent_id: str) -> dict:
        """Return computed progress for the children of *parent_id*.

        Counts + Kahn-decomposition waves over ``blocks`` edges among the
        children — never stored, always recomputed (work-graph §4.1).
        Shape::

            {
                "parent_id": ...,
                "total": int,
                "done": int,        # COMPLETED
                "ready": int,       # READY ∧ is_blocked = 0
                "blocked": int,     # is_blocked = 1
                "in_progress": int, # ASSIGNED / IN_PROGRESS
                "waves": list[list[task_id]],
            }
        """
        children = await self.get_subtasks(parent_id)
        counts = {"done": 0, "ready": 0, "blocked": 0, "in_progress": 0}
        for c in children:
            status = getattr(c.status, "value", c.status)
            if status == TaskStatus.COMPLETED.value:
                counts["done"] += 1
            elif getattr(c, "is_blocked", False):
                counts["blocked"] += 1
            elif status == TaskStatus.READY.value:
                counts["ready"] += 1
            elif status in (TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value):
                counts["in_progress"] += 1

        # Kahn waves over ``blocks`` edges internal to the child set.
        child_ids = {c.id for c in children}
        indeg: dict[str, int] = {cid: 0 for cid in child_ids}
        adj: dict[str, list[str]] = {cid: [] for cid in child_ids}
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(
                        task_dependencies.c.task_id,
                        task_dependencies.c.depends_on_task_id,
                    ).where(
                        and_(
                            task_dependencies.c.dep_type == "blocks",
                            task_dependencies.c.task_id.in_(sorted(child_ids)),
                            task_dependencies.c.depends_on_task_id.in_(sorted(child_ids)),
                        )
                    )
                )
            ).fetchall()
        for tid, dep in rows:
            adj[dep].append(tid)
            indeg[tid] = indeg.get(tid, 0) + 1

        waves: list[list[str]] = []
        current = sorted(cid for cid, d in indeg.items() if d == 0)
        while current:
            waves.append(current)
            next_wave: list[str] = []
            for cid in current:
                for nb in adj.get(cid, []):
                    indeg[nb] -= 1
                    if indeg[nb] == 0:
                        next_wave.append(nb)
            current = sorted(next_wave)

        return {
            "parent_id": parent_id,
            "total": len(children),
            **counts,
            "waves": waves,
            "max_parallelism": max((len(w) for w in waves), default=0),
            "depth": len(waves),
        }

    async def get_subtasks(self, parent_task_id: str) -> list[Task]:
        """Return all direct children of a task."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(tasks).where(tasks.c.parent_task_id == parent_task_id)
            )
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def get_parent_tasks(
        self,
        project_id: str,
        *,
        labels: list[str] | None = None,
        any_label: list[str] | None = None,
    ) -> list[Task]:
        """Return top-level tasks for a project (those with no parent).

        ``labels`` (all-of) and ``any_label`` (any-of) apply the same filters
        as :meth:`list_tasks`, so tree/compact listings can honour a label
        filter instead of silently ignoring it.
        """
        stmt = select(tasks).where(
            (tasks.c.project_id == project_id) & (tasks.c.parent_task_id.is_(None))
        )
        if labels or any_label:
            stmt = apply_label_filters(stmt, labels=labels, any_label=any_label)
        stmt = stmt.order_by(tasks.c.priority.asc(), tasks.c.created_at.asc())
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def find_task_by_dedup_key(self, project_id: str, dedup_key: str) -> "Task | None":
        """Return the non-terminal task with (project_id, dedup_key), or None.

        Terminal statuses (COMPLETED / FAILED) are ignored so a
        completed dedup key does not perpetually squat.
        """
        terminal = (
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
        )
        stmt = (
            select(tasks)
            .where(
                and_(
                    tasks.c.project_id == project_id,
                    tasks.c.dedup_key == dedup_key,
                    tasks.c.status.notin_(terminal),
                )
            )
            .order_by(tasks.c.created_at.asc())
            .limit(1)
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).mappings().fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    @staticmethod
    def _row_to_task(row) -> Task:
        """Convert a database row to a Task model."""
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            parent_task_id=row["parent_task_id"],
            repo_id=row["repo_id"],
            title=row["title"],
            description=row["description"],
            priority=row["priority"],
            status=TaskStatus(row["status"]),
            verification_type=VerificationType(row["verification_type"]),
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            assigned_agent_id=row["assigned_agent_id"],
            branch_name=row["branch_name"],
            resume_after=row["resume_after"],
            requires_approval=bool(row["requires_approval"]),
            pr_url=row["pr_url"],
            plan_source=row["plan_source"],
            is_plan_subtask=bool(row["is_plan_subtask"]),
            task_type=TaskType(row["task_type"]) if row["task_type"] else None,
            profile_id=row["profile_id"],
            preferred_workspace_id=row["preferred_workspace_id"],
            attachments=json.loads(row["attachments"]) if row["attachments"] else [],
            auto_approve_plan=bool(row["auto_approve_plan"]),
            skip_verification=bool(row.get("skip_verification", 0)),
            workflow_id=row.get("workflow_id"),
            affinity_agent_id=row.get("affinity_agent_id"),
            affinity_reason=row.get("affinity_reason"),
            workspace_mode=(
                WorkspaceMode(row["workspace_mode"]) if row.get("workspace_mode") else None
            ),
            is_blocked=bool(row.get("is_blocked", 0)),
            created_at=row.get("created_at", 0.0),
            updated_at=row.get("updated_at", 0.0),
            dedup_key=row.get("dedup_key"),
            discord_thread_id=row.get("discord_thread_id"),
            intelligence_class=row.get("intelligence_class"),
            created_by_kind=row.get("created_by_kind"),
            created_by_id=row.get("created_by_id"),
            claim_epoch=int(row.get("claim_epoch") or 0),
            filed_count=int(row.get("filed_count") or 0),
        )

    async def update_task_routing(
        self,
        task_id: str,
        *,
        profile_id: str | None,
        intelligence_class: str | None,
        preferred_workspace_id: str | None,
        clear_intelligence_class: bool = False,
    ) -> bool:
        """Update routing only while no worker holds the task.

        The predicate is in the write itself so a claim that wins after the
        command's read cannot be silently retargeted. Nullable values retain
        existing overrides unless editing explicitly clears the class.
        """
        vals: dict = {"profile_id": profile_id}
        if intelligence_class is not None or clear_intelligence_class:
            vals["intelligence_class"] = intelligence_class
        if preferred_workspace_id is not None:
            vals["preferred_workspace_id"] = preferred_workspace_id
        active_session = select(sessions.c.id).where(
            sessions.c.task_id == tasks.c.id,
            sessions.c.state.in_(("starting", "running", "draining")),
        ).exists()
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(tasks).where(
                    tasks.c.id == task_id,
                    tasks.c.status != TaskStatus.IN_PROGRESS.value,
                    tasks.c.assigned_agent_id.is_(None),
                    ~active_session,
                ).values(**vals)
            )
        return result.rowcount == 1
