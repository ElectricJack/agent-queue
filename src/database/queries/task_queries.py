"""Task CRUD and filtering operations."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, insert, select, update, func, and_

from src.database.tables import (
    gates,
    sessions,
    task_context,
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
from src.models import Task, TaskStatus, TaskType, VerificationType, WorkspaceMode
from src.state_machine import is_valid_status_transition

logger = logging.getLogger(__name__)


@dataclass
class TransitionResult:
    """What one status write changed besides the row itself."""

    flipped: set[str] = field(default_factory=set)
    settled: list[str] = field(default_factory=list)
    #: Descendant ids closed as ``abandoned`` — populated only by
    #: ``HierarchyQueryMixin.abandon_subtree`` (spec §7); empty otherwise.
    abandoned: list[str] = field(default_factory=list)


class TaskQueryMixin:
    """Query mixin for task operations.  Expects ``self._engine``."""

    async def create_task(self, task: Task) -> None:
        """Insert a new task row."""
        async with self._engine.begin() as conn:
            await self._insert_task_row(task, conn=conn)

    async def _insert_task_row(self, task: Task, *, conn) -> None:
        """Insert a single task row.  Caller owns the transaction."""
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
        """
        values = self._coerce_task_values(kwargs)
        result = TransitionResult()

        row = (await conn.execute(select(tasks.c.status).where(tasks.c.id == task_id))).fetchone()

        if row is None:
            logger.warning("transition_task: task '%s' not found, cannot validate", task_id)
            values["status"] = new_status.value
            values["updated_at"] = time.time()
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))
            return TransitionResult()

        current_status = TaskStatus(row[0])

        if current_status == new_status:
            if values:
                values["updated_at"] = time.time()
                await conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))
                if PROJECTION_INPUT_COLUMNS & values.keys():
                    result.flipped = await self.recompute_blocked({task_id}, conn=conn)
        else:
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
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))
            result.flipped = await self.recompute_blocked({task_id}, conn=conn)
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

    async def transition_task(
        self,
        task_id: str,
        new_status: TaskStatus,
        *,
        context: str = "",
        event=None,
        force: bool = False,
        **kwargs,
    ) -> set[str]:
        """Public status write: one transaction, then post-commit emission.

        Returns the blocked-state flips (unchanged contract).  Settled
        containers are delivered to the settlement listener after commit.
        """
        async with self._engine.begin() as conn:
            result = await self._apply_transition(
                conn, task_id, new_status, context=context, event=event, force=force, **kwargs
            )
        await self.log_blocked_flips(result.flipped)
        await self._notify_settled(result.settled)
        return result.flipped

    #: Statuses after which a task will not run again, so anything gated on
    #: it can never be satisfied by waiting.
    _TERMINAL_TASK_STATUSES = ("COMPLETED", "FAILED", "CANCELLED")

    #: Post-commit settlement callback, registered via ``set_settlement_listener``.
    _settlement_listener = None

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

    async def delete_task(self, task_id: str) -> None:
        """Delete a task and all related child rows.

        The former dependents (and any fan-in waiters that reach this task
        through a container) are snapshotted before the edges disappear and
        recomputed in the same transaction.

        Gates the task was waiting on are unhooked here too, and any gate
        left with no waiters at all is marked ``expired``. Without this the
        ``task_gates`` FK (``NO ACTION``) simply refuses the delete, and
        force-removing those rows instead strands the gate ``open`` forever
        — which for a human gate means live Approve/Deny buttons in Discord
        for a task that no longer exists.
        """
        async with self._engine.begin() as conn:
            # Snapshot everything whose projection this deletion can change,
            # *while the edges still exist*.
            affected = await self._collect_affected({task_id}, conn)
            affected.discard(task_id)

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
                        .values(status="expired", resolution="last waiter task deleted")
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
                .values(locked_by_task_id=None, locked_by_agent_id=None)
            )

            await conn.execute(delete(tasks).where(tasks.c.id == task_id))

            flipped = await self.recompute_blocked(affected, conn=conn) if affected else set()
        await self.log_blocked_flips(flipped)

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

    async def remove_task_label(self, task_id: str, label: str) -> None:
        """Detach a label from a task. No-op if not present."""
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(task_labels).where(
                    and_(task_labels.c.task_id == task_id, task_labels.c.label == label)
                )
            )

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
        }

    async def get_subtasks(self, parent_task_id: str) -> list[Task]:
        """Return all direct children of a task."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(tasks).where(tasks.c.parent_task_id == parent_task_id)
            )
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def get_task_tree(self, root_task_id: str) -> dict | None:
        """Return a nested dict representing the full task hierarchy."""
        root = await self.get_task(root_task_id)
        if root is None:
            return None

        async def _build_subtree(task: Task) -> dict:
            children = await self.get_subtasks(task.id)
            child_nodes = []
            for child in children:
                child_nodes.append(await _build_subtree(child))
            return {"task": task, "children": child_nodes}

        return await _build_subtree(root)

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
        profile_id: str,
        intelligence_class: str | None,
        preferred_workspace_id: str | None,
    ) -> None:
        """Set profile + intelligence class + optional preferred workspace.

        Used by ``_cmd_task_route`` (dv2 phase 1) to commit routing
        decisions before resolving the ``routing`` gate on the task.
        Nullable fields are only touched when the caller passes a value;
        this keeps ``task_route`` narrow — it never accidentally clears
        an already-set ``intelligence_class`` or ``preferred_workspace_id``.
        """
        vals: dict = {"profile_id": profile_id}
        if intelligence_class is not None:
            vals["intelligence_class"] = intelligence_class
        if preferred_workspace_id is not None:
            vals["preferred_workspace_id"] = preferred_workspace_id
        async with self._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(**vals))
