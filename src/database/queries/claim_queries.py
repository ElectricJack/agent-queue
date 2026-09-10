"""Claims — who holds what (swarm-work-model §10, §11.2).

Every write that records a holder goes through this mixin on a caller-owned
connection opened by ``immediate()``: the session slot is taken with a
conditional UPDATE first (row lock on Postgres, writer lock on SQLite), then
the task, then agent/workspace/metadata.  ``activate_claim`` and
``release_claim`` are the two ways a claim leaves ``preparing`` and they
race by design — the conditional UPDATEs make exactly one of them win.
"""

from __future__ import annotations

import json
import time

from sqlalchemy import Float, and_, case, cast, delete, exists, false, func, literal, select, update

from src.database.queries.blocked_state import apply_label_filters
from src.database.queries.hierarchy_queries import (
    ProjectIntegrationMode,
    container_flag_exists,
    delivered_same_parent_prerequisites_when_hierarchical,
    materialized_origin_when_hierarchical,
)
from src.database.queries.session_queries import _row_to_session
from src.database.queries.task_queries import (
    ManualPauseActive,
    TransitionResult,
    _not_manually_paused,
    supports_returning,
)
from src.database.tables import (
    agents,
    integration_branch_owners,
    integration_repair_stages,
    sessions,
    task_metadata,
    task_workspace_requirements,
    tasks,
    workspaces,
)
from src.models import AgentState, SessionRecord, Task, TaskEvent, TaskStatus, Workspace


def _frontier_where(project_id: str, hierarchy_mode: ProjectIntegrationMode | None = None):
    """The frontier predicate, for one project.

    *hierarchy_mode* is this project's already-read
    ``hierarchical_integration_mode`` / ``integration_repository_id`` pair.
    Supplying it folds the two hierarchy predicates' ``projects`` lookups
    into constants instead of re-asking the same one-row question once per
    candidate row; ``None`` keeps the self-contained correlated form.
    """
    return and_(
        tasks.c.project_id == project_id,
        tasks.c.status == TaskStatus.READY.value,
        tasks.c.is_blocked == 0,
        tasks.c.assigned_agent_id.is_(None),
        tasks.c.is_plan_subtask == 0,
        materialized_origin_when_hierarchical(hierarchy_mode),
        delivered_same_parent_prerequisites_when_hierarchical(hierarchy_mode),
        # A flagged container (spec §7) has no deliverable of its own: it is
        # released to IN_PROGRESS by the orchestrator and settles when its
        # children finish.  A worker holding it could never close it
        # (Invariant 6) and its live session would block the settlement it
        # waits for (calm-ember-48).
        ~container_flag_exists(),
        # A repair delegate belongs to one exact active stage.  When that
        # stage expires, the task row can still be READY until cleanup runs;
        # it must not be offered to a pool worker in that gap.
        ~exists(
            select(literal(1)).where(
                integration_repair_stages.c.repair_task_id == tasks.c.id,
                integration_repair_stages.c.writer_kind == "repair_delegate",
                integration_repair_stages.c.state.notin_(("active", "awaiting_completion")),
            )
        ),
    )


# Kept in task metadata rather than a task column: this is operational
# claim-state, not lifecycle state, and therefore needs no schema migration.
PREPARE_BACKOFF_UNTIL_KEY = "claim_prepare_backoff_until"
PREPARE_BACKOFF_ATTEMPTS_KEY = "claim_prepare_backoff_attempts"
PREPARE_BACKOFF_INITIAL_SECONDS = 120.0
PREPARE_BACKOFF_MAX_SECONDS = 300.0
CLAIM_PREPARATION_METADATA_KEYS = (
    "manual_pause_checkpoint",
    PREPARE_BACKOFF_UNTIL_KEY,
    PREPARE_BACKOFF_ATTEMPTS_KEY,
    "slot_reset_failure",
)

#: Matches exactly what PostgreSQL's ``double precision`` input accepts here:
#: an optional sign, digits, and an optional fractional part.  Deliberately
#: narrower than ``float8`` (no exponents, no ``NaN``/``Infinity``) because
#: every writer of a numeric metadata value writes a plain timestamp.
_NUMERIC_TEXT = r"^-?[0-9]+(\.[0-9]+)?$"


def numeric_meta_value(column, *, default: str = "0"):
    """``column`` cast to ``Float``, with non-numeric text read as *default*.

    ``task_metadata.value`` is free-form JSON text: ``json.dumps`` writes a
    bare ``0`` for the number and ``"0"`` -- quotes included -- for the
    string, and nothing in the schema stops a caller storing either.  A bare
    ``cast(value, Float)`` therefore raises
    ``invalid input syntax for type double precision`` on the *whole
    statement* the moment one malformed row is scanned.

    That is not a hypothetical.  On 2026-09-07 two rows holding ``"0"`` made
    every ``select_ready_for_profile`` call in one project raise for ~18
    hours: no pool worker could claim anything, and because the failure was
    a database error rather than an empty result, the queue looked idle
    rather than broken.

    The ``CASE`` is what makes this safe rather than merely likely to work:
    PostgreSQL does not guarantee evaluation order between a regex guard and
    a cast sitting in the same ``AND``, so the guard has to be *inside* the
    expression being cast.  A malformed row then reads as *default* -- for a
    backoff deadline, "expired", which fails open to claimable rather than
    silently withholding work.
    """
    return cast(
        case((column.op("~")(_NUMERIC_TEXT), column), else_=literal(default)),
        Float,
    )


class ClaimQueryMixin:
    """Expects ``self._engine`` plus Task/Session/Workspace/Hierarchy mixins.

    ``_row_to_workspace`` (WorkspaceQueryMixin), ``_apply_transition`` /
    ``_row_to_task`` (TaskQueryMixin) and ``_upsert_meta[_many]``
    (HierarchyQueryMixin) all come from the composed adapter.
    """

    async def take_claim_slot(self, conn, session_id: str, *, now: float, cap: int | None):
        """CAS the session into ``claiming``; ``(kind, session_or_None)``.

        The happy path is **one** statement: ``UPDATE … RETURNING`` hands
        back the row it just took, so the re-read below only runs when the
        CAS lost (or the dialect predates RETURNING — SQLite < 3.35).
        """
        cond = [
            sessions.c.id == session_id,
            sessions.c.task_id.is_(None),
            sessions.c.claim_phase.is_(None),
            sessions.c.desired_state == "running",
        ]
        if cap is not None:
            cond.append(sessions.c.claims < cap)
        stmt = (
            update(sessions).where(and_(*cond)).values(claim_phase="claiming", claim_phase_at=now)
        )
        row = None
        if supports_returning(conn):
            row = (await conn.execute(stmt.returning(*sessions.c))).mappings().fetchone()
            took = row is not None
        else:
            took = (await conn.execute(stmt)).rowcount == 1
        if row is None:
            row = (
                (await conn.execute(select(sessions).where(sessions.c.id == session_id)))
                .mappings()
                .fetchone()
            )
        if row is None:
            return "not_found", None
        record = _row_to_session(row)
        if took:
            return "slot", record
        if record.claim_phase in ("active", "preparing", "claiming"):
            return record.claim_phase, record
        if record.desired_state != "running":
            return "drain_requested", record
        if cap is not None and record.claims >= cap:
            return "session_exhausted", record
        if record.task_id:
            return "active", record
        return "not_found", record

    async def claim_preparation_is_current(
        self, session_id: str, task_id: str, claim_epoch: int, *, conn=None
    ) -> bool:
        """Verify the task and pool-session fences before filesystem work.

        Slot reset and claim-file creation happen outside the claim
        transaction, so this read must prove both rows are still current.
        Joining their two predicates avoids two independent round trips on
        every successful pool claim.

        *conn* lets the caller pair this with its ``get_project`` re-read on
        one checkout — the two run back to back under the task control lock.
        """
        stmt = (
            select(literal(1))
            .select_from(tasks.join(sessions, sessions.c.task_id == tasks.c.id))
            .where(
                tasks.c.id == task_id,
                tasks.c.status == TaskStatus.IN_PROGRESS.value,
                tasks.c.claim_epoch == claim_epoch,
                sessions.c.id == session_id,
                sessions.c.claim_phase == "preparing",
                sessions.c.desired_state == "running",
            )
        )
        if conn is not None:
            return (await conn.execute(stmt)).scalar_one_or_none() is not None
        async with self._engine.connect() as owned:
            return (await owned.execute(stmt)).scalar_one_or_none() is not None

    async def release_claim_slot(self, conn, session_id: str) -> None:
        await conn.execute(
            update(sessions)
            .where(and_(sessions.c.id == session_id, sessions.c.claim_phase == "claiming"))
            .values(claim_phase=None, claim_phase_at=None)
        )

    async def select_ready_for_profile(
        self,
        conn,
        *,
        project_id,
        profile_id,
        default_profile_id,
        agent_id,
        task_id=None,
        enforce_routing=False,
        intelligence_class=None,
        llm_provider=None,
        options_hash=None,
        hierarchy_mode: ProjectIntegrationMode | None = None,
    ) -> str | None:
        """The §10 work query.  Postgres takes the row FOR UPDATE SKIP LOCKED.

        *hierarchy_mode* is the caller's already-read project row, reduced to
        the two constants the hierarchy predicates need (see
        :class:`ProjectIntegrationMode`).  It is only ever a *frontier*
        filter: whichever candidate this returns is re-fenced under the task
        lock afterwards, and ``_prepare_and_activate_locked`` re-reads the
        project before acting on its mode, so a mode edit that lands inside a
        long ``--wait`` window cannot be acted on from a stale read here.

        Two statements, not one, and the reason is the ``ORDER BY``.  Affinity
        is a *soft* preference -- the frontier does not exclude a task pinned
        to another agent -- and it used to be expressed as the leading sort
        key, ``CASE WHEN affinity_agent_id = :agent THEN 0 ELSE 1 END``.  The
        agent id is a runtime parameter, so no index can supply that order:
        PostgreSQL had to evaluate the whole frontier predicate for every
        candidate row and top-N sort the result, which at the §15.2 scale
        (5,000 tasks, 2,499 on the frontier) meant 2,500 rows materialised,
        the correlated ``NOT EXISTS`` subplans in :func:`_frontier_where`
        evaluated 2,500 times, and ~10,100 shared buffers per claim -- for a
        ``LIMIT 1``.

        Sorting by ``priority, created_at`` alone *is* index-orderable, so
        each half below is an index-ordered scan that stops at the first
        admissible row (10-14 shared buffers, measured on PostgreSQL 18 at
        that same scale).  Splitting the preference across two statements
        preserves it exactly:

        * the first statement is the frontier restricted to tasks pinned to
          this agent -- if any admissible pinned task exists, the old sort
          would have returned the best of them, and so does this;
        * the second is the frontier with no affinity term at all -- reached
          only when the first found nothing, which is exactly when the old
          sort's leading key was constant and the answer was the best row
          overall.

        ``SKIP LOCKED`` falls through the same way it always did: a pinned row
        another claimer holds is skipped by the first statement, and if every
        pinned row is held the second statement still considers unpinned work.
        That fall-through is the reason this is two statements rather than one
        with the probe hoisted into an ``InitPlan`` and used to *restrict* the
        frontier -- that shape is a statement cheaper and measures the same,
        but it answers ``no_ready_work`` when every pinned row is momentarily
        locked, and the caller then parks on the ``task.ready`` waiter for the
        rest of its ``--wait`` window rather than taking the unpinned work
        that was there all along.

        A targeted claim (*task_id* given) skips the probe: with a single
        candidate row, preferring it over itself is a no-op.
        """
        profile_ok = tasks.c.profile_id == profile_id
        if default_profile_id == profile_id and not enforce_routing:
            profile_ok = (tasks.c.profile_id == profile_id) | tasks.c.profile_id.is_(None)
        req = task_workspace_requirements.alias("req")
        prepare_backoff_active = exists(
            select(literal(1)).where(
                task_metadata.c.task_id == tasks.c.id,
                task_metadata.c.key == PREPARE_BACKOFF_UNTIL_KEY,
                numeric_meta_value(task_metadata.c.value) > time.time(),
            )
        )

        def candidate(*, pinned: bool):
            stmt = (
                select(tasks.c.id)
                .where(
                    _frontier_where(project_id, hierarchy_mode),
                    profile_ok,
                    ~exists(
                        select(literal(1)).where(
                            and_(
                                req.c.task_id == tasks.c.id,
                                req.c.kind_id.notin_(("project-repo", "vault")),
                            )
                        )
                    ),
                    ~prepare_backoff_active,
                )
                .order_by(
                    tasks.c.priority.asc(),
                    tasks.c.created_at.asc(),
                )
                .limit(1)
            )
            if pinned:
                stmt = stmt.where(tasks.c.affinity_agent_id == agent_id)
            stmt = apply_label_filters(stmt, exclude_hold=True)
            if enforce_routing:
                # The task row is the route (assignment-routing-as-playbook
                # spec §2): a worker fixed on a class takes only tasks
                # carrying that class.  An explicit class never inherits a
                # provider pin.
                explicit_class = func.nullif(func.trim(tasks.c.intelligence_class), "")
                stmt = stmt.where(
                    explicit_class == intelligence_class if intelligence_class else false(),
                )
            if task_id is not None:
                stmt = stmt.where(tasks.c.id == task_id)
            return stmt.with_for_update(of=tasks, skip_locked=True)

        if task_id is None:
            row = (await conn.execute(candidate(pinned=True))).fetchone()
            if row:
                return row[0]
        row = (await conn.execute(candidate(pinned=False))).fetchone()
        return row[0] if row else None

    async def take_task(self, conn, task_id: str, *, agent_id: str, now: float) -> Task | None:
        """Fence + epoch bump + status write in **one** statement (spec §15).

        The fence (``READY``, unblocked, unassigned) rides the same UPDATE as
        the epoch bump and the ``IN_PROGRESS`` write, so exactly one racer
        can match it; a matched row proves the pre-state, which is what lets
        ``_apply_transition`` skip its pre-read (``assume_pre_state``).  The
        write still goes through ``_apply_transition`` — the single
        sanctioned status-write path — which validates
        ``READY --CLAIMED--> IN_PROGRESS`` on the state machine and skips the
        blocked-state recompute: no clause of ``blocked_predicate()`` can
        tell READY from IN_PROGRESS, so nothing's projection can move
        (``projection_stable``).  Returns ``None`` when the fence lost.
        """
        # Reserving the worker is also its soft-delete fence.  The old
        # SELECT ... FOR UPDATE followed by record_holder's later UPDATE
        # needed two round trips to lock and mark the same row.  This guarded
        # UPDATE does both, while retaining the lock until the task transition
        # commits; a competing soft delete can neither pass its idle predicate
        # nor alter this row underneath the claim.
        reserve = (
            update(agents)
            .where(
                agents.c.id == agent_id,
                agents.c.enabled.is_(True),
                agents.c.role == "worker",
                agents.c.deleted_at.is_(None),
                agents.c.state == AgentState.IDLE.value,
                agents.c.current_task_id.is_(None),
            )
            .values(state=AgentState.BUSY.value, current_task_id=task_id)
        )
        if supports_returning(conn):
            reserved = (
                await conn.execute(reserve.returning(agents.c.id))
            ).scalar_one_or_none() is not None
        else:
            reserved = (await conn.execute(reserve)).rowcount == 1
        if not reserved:
            return None
        out = await self._apply_transition(
            conn,
            task_id,
            TaskStatus.IN_PROGRESS,
            context="claim",
            event=TaskEvent.CLAIMED,
            assigned_agent_id=agent_id,
            projection_stable=True,
            assume_pre_state=(TaskStatus.READY, False),
            extra_where=and_(
                tasks.c.status == TaskStatus.READY.value,
                tasks.c.is_blocked == 0,
                tasks.c.assigned_agent_id.is_(None),
                ~exists(
                    select(literal(1)).where(
                        integration_repair_stages.c.repair_task_id == tasks.c.id,
                        integration_repair_stages.c.writer_kind == "repair_delegate",
                        integration_repair_stages.c.state.notin_(
                            ("active", "awaiting_completion")
                        ),
                    )
                ),
            ),
            extra_values={"claim_epoch": tasks.c.claim_epoch + 1},
            returning=True,
        )
        if out.row is None:
            await conn.execute(
                update(agents)
                .where(agents.c.id == agent_id, agents.c.current_task_id == task_id)
                .values(state=AgentState.IDLE.value, current_task_id=None)
            )
            return None
        return self._row_to_task(out.row)

    async def bump_claim_epoch(self, task_id: str, *, conn=None) -> int:
        async def _run(c):
            changed = await c.execute(
                update(tasks)
                .where(tasks.c.id == task_id, _not_manually_paused())
                .values(claim_epoch=tasks.c.claim_epoch + 1)
            )
            if changed.rowcount == 0:
                raise ManualPauseActive(f"Task {task_id} is paused or missing; cannot launch.")
            return (
                await c.execute(select(tasks.c.claim_epoch).where(tasks.c.id == task_id))
            ).scalar() or 0

        if conn is not None:
            return await _run(conn)
        async with self.immediate() as conn:
            return await _run(conn)

    async def record_holder(
        self,
        conn,
        *,
        session_id,
        task_id,
        claim_epoch,
        agent_id,
        work_dir,
        now,
        agent_reserved=False,
    ) -> Workspace | None:
        """Write the holder rows; return the agent's workspace slot.

        The workspace UPDATE uses ``RETURNING`` so the caller
        (``_prepare_and_activate``) does not have to re-read the slot it
        just stamped, and both metadata keys go out in one multi-row upsert
        (spec §15).
        """
        await conn.execute(
            update(sessions)
            .where(sessions.c.id == session_id)
            .values(
                task_id=task_id,
                claim_phase="preparing",
                claim_phase_at=now,
                last_claim_epoch=claim_epoch,
            )
        )
        slot = None
        if agent_id:
            if not agent_reserved:
                await conn.execute(
                    update(agents)
                    .where(agents.c.id == agent_id)
                    .values(state=AgentState.BUSY.value, current_task_id=task_id)
                )
            # Pool sessions keep the agent lock for their lifetime; claiming
            # another task only changes the task hold within that same slot.
            stmt = (
                update(workspaces)
                .where(
                    workspaces.c.project_id
                    == select(sessions.c.project_id)
                    .where(sessions.c.id == session_id)
                    .scalar_subquery(),
                    workspaces.c.workspace_path == work_dir,
                    workspaces.c.enabled.is_(True),
                    workspaces.c.locked_by_agent_id == agent_id,
                )
                .values(
                    locked_by_agent_id=agent_id,
                    locked_by_task_id=task_id,
                    locked_at=now,
                )
            )
            if supports_returning(conn):
                row = (await conn.execute(stmt.returning(*workspaces.c))).mappings().fetchone()
                slot = self._row_to_workspace(row) if row is not None else None
            else:
                await conn.execute(stmt)
                row = (
                    (
                        await conn.execute(
                            select(workspaces).where(
                                workspaces.c.project_id
                                == select(sessions.c.project_id)
                                .where(sessions.c.id == session_id)
                                .scalar_subquery(),
                                workspaces.c.workspace_path == work_dir,
                                workspaces.c.locked_by_agent_id == agent_id,
                            )
                        )
                    )
                    .mappings()
                    .fetchone()
                )
                slot = self._row_to_workspace(row) if row is not None else None
        await self._upsert_meta_many(
            task_id, {"claimed_by_session": session_id, "work_dir": work_dir}, conn=conn
        )
        await self._start_task_session_attempt(
            conn,
            session_id,
            started_at=now,
            work_dir=work_dir,
        )
        return slot

    async def activate_claim(
        self, session_id, task_id, *, epoch: int, now: float, conn=None,
        branch_name: str | None = None,
        clear_preparation_metadata: bool = False,
    ) -> SessionRecord | None:
        """Flip ``preparing`` -> ``active``; the updated row, or ``None``.

        Returning the row (via ``RETURNING`` where the dialect has it) saves
        the caller a re-read to build the response's session block.  Falsy
        on failure, so the old ``if not await activate_claim(...)`` callers
        read unchanged.

        *branch_name* is the branch the claim's slot reset just put the
        worktree on.  It is persisted to ``tasks.branch_name`` under the same
        row lock and in the same transaction as the activation, so a claim
        either publishes both or neither: a prepare that fails after the
        reset leaves no half-written branch for a later close to trust.  The
        push-assignment path writes the same field from
        ``WorkspaceMixin._assign_worktree_slot``; the pool-claim path used to
        discard it, which left ``branch_name`` NULL on every development-mode
        pool task and made its close refuse forever in
        ``resolve_workspace_checkpoint``.

        *clear_preparation_metadata* folds the successful-preparation
        cleanup — the pause checkpoint and prepare-backoff keys in
        ``CLAIM_PREPARATION_METADATA_KEYS``, which all go stale at exactly
        this boundary — into the ``needs_attention`` delete below.  It used
        to be a separate ``clear_claim_preparation_metadata`` call after this
        transaction committed, which cost a whole extra pooled checkout (~6
        statements' worth of wire) *and* a second delete on the same table
        for the same task.  Riding along here also makes the two atomic:
        there is no longer a window in which a claim is active while its
        backoff ladder still says the last preparation failed.  It is inside
        every activation guard, so an activation that bails clears nothing.
        """

        async def _run(c):
            # Claims and release acquire the session before the task. Keep
            # activation in that order as well to avoid a PostgreSQL deadlock.
            holder = (
                await c.execute(
                    select(sessions.c.id)
                    .where(
                        sessions.c.id == session_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if holder is None:
                return None
            # Share the task row lock with pause. An EXISTS predicate alone
            # can observe a pre-pause PostgreSQL statement snapshot.
            claim = (
                await c.execute(
                    select(tasks.c.id, tasks.c.branch_name)
                    .where(
                        tasks.c.id == task_id,
                        tasks.c.status == TaskStatus.IN_PROGRESS.value,
                        tasks.c.claim_epoch == epoch,
                    )
                    .with_for_update()
                )
            ).fetchone()
            if claim is None:
                return None
            stmt = (
                update(sessions)
                .where(
                    and_(
                        sessions.c.id == session_id,
                        sessions.c.claim_phase == "preparing",
                        sessions.c.task_id == task_id,
                        sessions.c.desired_state == "running",
                        sessions.c.state == "running",
                    )
                )
                .values(
                    claim_phase="active",
                    claim_phase_at=now,
                    claims=sessions.c.claims + 1,
                    last_claim_epoch=epoch,
                    last_claim_result="claimed",
                )
            )
            if supports_returning(c):
                row = (await c.execute(stmt.returning(*sessions.c))).mappings().fetchone()
            else:
                if (await c.execute(stmt)).rowcount != 1:
                    return None
                row = (
                    (await c.execute(select(sessions).where(sessions.c.id == session_id)))
                    .mappings()
                    .fetchone()
                )
            if row is None:
                return None
            # A claim only becomes usable after preparation has succeeded and
            # this session row has atomically moved to ``active``.  Clear an
            # earlier prepare/release warning at that exact point so it never
            # shadows a live task in the dashboard or recovery logic, and --
            # when the caller asks -- the preparation ladder that became
            # stale at the same boundary, in the same statement.
            stale_keys = ["needs_attention"]
            if clear_preparation_metadata:
                stale_keys.extend(CLAIM_PREPARATION_METADATA_KEYS)
            await c.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == task_id,
                    task_metadata.c.key.in_(stale_keys),
                )
            )
            if branch_name and claim.branch_name != branch_name:
                # Written only past every activation guard, and under the row
                # lock taken above: an activation that bails leaves the branch
                # exactly as it found it, so no failed prepare can publish a
                # branch the task never got.  Only a differing value is
                # written: a hierarchy claim's branch already comes from the
                # origin chain, and a no-op UPDATE would touch a task whose
                # materialized origin is deliberately frozen.
                await c.execute(
                    update(tasks)
                    .where(tasks.c.id == task_id)
                    .values(branch_name=branch_name, updated_at=now)
                )
            return _row_to_session(row)

        if conn is not None:
            return await _run(conn)
        async with self.immediate() as conn:
            return await _run(conn)

    async def _release_claim_on(
        self,
        conn,
        session_id,
        *,
        task_status,
        context,
        now,
        result,
        needs_attention,
        prepare_backoff=False,
        expected_task_id=None,
        expected_claim_epoch=None,
        expected_task_status=None,
        expected_task_claim_epoch=None,
        drain_after_release=False,
        release_workspace_lock=False,
        preserve_terminal_task=False,
        stop_after_release=False,
        end_reason=None,
    ) -> TransitionResult:
        row = (
            (
                await conn.execute(
                    select(sessions).where(sessions.c.id == session_id).with_for_update()
                )
            )
            .mappings()
            .fetchone()
        )
        out = TransitionResult()
        if row is None:
            return out
        task_id, agent_id = row["task_id"], row["agent_id"]
        # A task close may race pool reconciliation: the reconciler can
        # release the terminal hold and the worker can claim new work before
        # the original close resumes.  Never let that old close release the
        # successor's task or claim file.
        if (
            expected_task_id is not None
            and task_id != expected_task_id
            or expected_claim_epoch is not None
            and row["last_claim_epoch"] != expected_claim_epoch
        ):
            return out
        # An attached integration owner is durable evidence that this exact
        # session is still responsible for its workspace.  Do not clear the
        # claim or either workspace binding until its handoff completes: a
        # pool reconciler can otherwise destroy the evidence a repair or
        # integration close needs.  Lock the owner row in this transaction so
        # the decision composes with ownership handoff on Postgres too.
        if agent_id:
            protected_owner = (
                await conn.execute(
                    select(integration_branch_owners.c.id)
                    .join(
                        workspaces,
                        integration_branch_owners.c.workspace_id == workspaces.c.id,
                    )
                    .where(
                        integration_branch_owners.c.session_id == session_id,
                        integration_branch_owners.c.handoff_state.in_(
                            ("attached", "handoff_pending")
                        ),
                        workspaces.c.locked_by_agent_id == agent_id,
                    )
                    .with_for_update()
                )
            ).first()
            if protected_owner is not None:
                return out
        epoch = None
        if task_id:
            if preserve_terminal_task:
                # A close can commit the terminal task transition before it
                # loses its response during a daemon restart.  Recovery must
                # free only the stale holder -- re-applying COMPLETED would
                # rewrite completion timing/context and blur the reviewed
                # terminal record it is meant to preserve.
                terminal = (
                    await conn.execute(
                        select(tasks.c.claim_epoch)
                        .where(
                            tasks.c.id == task_id,
                            tasks.c.status == task_status.value,
                            tasks.c.claim_epoch == expected_claim_epoch,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if terminal is None:
                    return out
                epoch = terminal
            else:
                # ``projection_stable``: IN_PROGRESS -> READY cannot move any
                # task's ``is_blocked`` (see ``_PROJECTION_NEUTRAL_STATUSES``);
                # it is ignored for every other target status, so the FAILED /
                # BLOCKED releases keep the full recompute.  ``returning`` folds
                # what used to be a separate ``claim_epoch`` read into the write.
                transition = dict(
                    context=context,
                    force=True,
                    assigned_agent_id=None,
                    projection_stable=True,
                    returning=True,
                    # Releasing the worker's ownership must not resume or alter
                    # an explicit manual pause.  It only clears its stale agent
                    # assignment after a task moved out from under the claim.
                    _manual_pause_control=task_status is TaskStatus.PAUSED,
                )
                if expected_task_status is not None:
                    guards = [tasks.c.status == expected_task_status.value]
                    if expected_task_claim_epoch is not None:
                        guards.append(tasks.c.claim_epoch == expected_task_claim_epoch)
                    transition["extra_where"] = and_(*guards)
                elif task_status == TaskStatus.READY:
                    transition.update(
                        assume_pre_state=(TaskStatus.IN_PROGRESS, False),
                        extra_where=and_(
                            tasks.c.status == TaskStatus.IN_PROGRESS.value,
                            tasks.c.is_blocked == 0,
                        ),
                    )
                out = await self._apply_transition(
                    conn,
                    task_id,
                    task_status,
                    **transition,
                )
                if expected_task_status is not None and out.row is None:
                    return out
                epoch = (out.row or {}).get("claim_epoch")
                if needs_attention:
                    await self._upsert_meta(task_id, "needs_attention", needs_attention, conn=conn)
                if prepare_backoff:
                    raw_attempts = (
                        await conn.execute(
                            select(task_metadata.c.value).where(
                                task_metadata.c.task_id == task_id,
                                task_metadata.c.key == PREPARE_BACKOFF_ATTEMPTS_KEY,
                            )
                        )
                    ).scalar_one_or_none()
                    try:
                        attempts = int(json.loads(raw_attempts)) if raw_attempts else 0
                    except (TypeError, ValueError, json.JSONDecodeError):
                        attempts = 0
                    attempts += 1
                    delay = min(
                        PREPARE_BACKOFF_INITIAL_SECONDS * (2 ** (attempts - 1)),
                        PREPARE_BACKOFF_MAX_SECONDS,
                    )
                    await self._upsert_meta_many(
                        task_id,
                        {
                            PREPARE_BACKOFF_ATTEMPTS_KEY: attempts,
                            PREPARE_BACKOFF_UNTIL_KEY: now + delay,
                        },
                        conn=conn,
                    )
        if task_id:
            await self.finish_task_session_attempt(
                session_id,
                task_id=task_id,
                ended_at=now,
                end_reason=end_reason or needs_attention or context,
                conn=conn,
            )
        if agent_id:
            # Clear the task lock unconditionally — even a session that held no
            # task (e.g. released mid-``claiming``) must not leave a stale
            # ``locked_by_task_id`` on its agent's workspace.  A task that
            # became non-live underneath an active worker must also release
            # the agent lock, so the slot can serve the next claim.
            await conn.execute(
                update(workspaces)
                .where(workspaces.c.locked_by_agent_id == agent_id)
                .values(
                    locked_by_agent_id=None if release_workspace_lock else workspaces.c.locked_by_agent_id,
                    locked_by_task_id=None,
                )
            )
            await conn.execute(
                update(agents)
                .where(agents.c.id == agent_id)
                .values(state=AgentState.IDLE.value, current_task_id=None)
            )
        session_values = dict(
            task_id=None,
            claim_phase=None,
            claim_phase_at=None,
            last_claim_epoch=epoch,
            last_claim_result=result,
        )
        if drain_after_release:
            session_values["desired_state"] = "stopped"
        if stop_after_release:
            session_values.update(
                state="stopped",
                desired_state="stopped",
                end_reason=end_reason or context,
                ended_at=now,
            )
        await conn.execute(
            update(sessions).where(sessions.c.id == session_id).values(**session_values)
        )
        out.released = True
        return out

    async def _after_release(self, out: TransitionResult) -> None:
        await self.log_blocked_flips(out.flipped)
        await self._notify_settled(out.settled)
        await self._notify_ready(out.ready)

    async def release_claim(
        self,
        session_id,
        *,
        task_status,
        context,
        now,
        result="released",
        needs_attention=None,
        expected_task_id=None,
        expected_claim_epoch=None,
        expected_task_status=None,
        expected_task_claim_epoch=None,
        drain_after_release=False,
        release_workspace_lock=False,
        prepare_backoff=False,
        preserve_terminal_task=False,
        stop_after_release=False,
        end_reason=None,
        conn=None,
    ) -> TransitionResult:
        kwargs = dict(
            task_status=task_status,
            context=context,
            now=now,
            result=result,
            needs_attention=needs_attention,
            expected_task_id=expected_task_id,
            expected_claim_epoch=expected_claim_epoch,
            expected_task_status=expected_task_status,
                expected_task_claim_epoch=expected_task_claim_epoch,
            drain_after_release=drain_after_release,
            release_workspace_lock=release_workspace_lock,
            prepare_backoff=prepare_backoff,
            preserve_terminal_task=preserve_terminal_task,
            stop_after_release=stop_after_release,
            end_reason=end_reason,
        )
        if conn is not None:
            return await self._release_claim_on(conn, session_id, **kwargs)
        async with self.immediate() as conn:
            out = await self._release_claim_on(conn, session_id, **kwargs)
        await self._after_release(out)
        return out

    async def release_historical_pool_claim(
        self,
        conn,
        session_id: str,
        *,
        task_id: str,
        claim_epoch: int,
        now: float,
    ) -> TransitionResult:
        """Release a stopped historical claim without touching its former holder.

        This is intentionally not a ``release_claim`` mode.  That normal path
        unwinds every workspace lock held by the session's agent and clears the
        agent's current task, which is correct for a live holder but corrupts a
        slot or agent that has since been reused.  The caller proves the
        detached integration handoff separately while holding its owner row;
        this method changes only the exact old task and exact old session.
        """
        row = (
            await conn.execute(
                select(sessions).where(sessions.c.id == session_id).with_for_update()
            )
        ).mappings().one_or_none()
        out = TransitionResult()
        if (
            row is None
            or row["task_id"] != task_id
            or row["lifecycle"] != "pool"
            or row["state"] != "stopped"
            or row["desired_state"] != "stopped"
            or row["claim_phase"] != "active"
            or row["last_claim_epoch"] != claim_epoch
        ):
            return out

        out = await self._apply_transition(
            conn,
            task_id,
            TaskStatus.PAUSED,
            context="integration_handoff_recovery",
            force=True,
            assigned_agent_id=None,
            _manual_pause_control=True,
            extra_where=and_(
                tasks.c.status == TaskStatus.BLOCKED.value,
                tasks.c.assigned_agent_id.is_(None),
                tasks.c.claim_epoch == claim_epoch,
            ),
            returning=True,
        )
        if out.row is None:
            return out
        released = await conn.execute(
            update(sessions)
            .where(
                sessions.c.id == session_id,
                sessions.c.task_id == task_id,
                sessions.c.lifecycle == "pool",
                sessions.c.state == "stopped",
                sessions.c.desired_state == "stopped",
                sessions.c.claim_phase == "active",
                sessions.c.last_claim_epoch == claim_epoch,
            )
            .values(
                task_id=None,
                claim_phase=None,
                claim_phase_at=None,
                last_claim_result="historical_handoff_recovered",
            )
        )
        if released.rowcount != 1:
            # The task transition and session release are one atomic repair.
            # A changed holder after the row was observed is not a partial
            # recovery; make the surrounding transaction roll back.
            raise RuntimeError("historical pool claim release lost its fence")
        await self.finish_task_session_attempt(
            session_id,
            task_id=task_id,
            ended_at=now,
            end_reason="integration_handoff_recovery",
            conn=conn,
        )
        out.released = True
        return out

    async def terminate_pool_session(
        self, session_id, *, reason, task_status=TaskStatus.READY, conn=None
    ) -> TransitionResult:
        async def _run(c):
            out = await self._release_claim_on(
                c,
                session_id,
                task_status=task_status,
                context=f"session_{reason}",
                end_reason=reason,
                now=time.time(),
                result="released",
                needs_attention=None,
            )
            if not out.released:
                return out
            row = (
                await c.execute(select(sessions.c.agent_id).where(sessions.c.id == session_id))
            ).fetchone()
            agent_id = row[0] if row else None
            if agent_id:
                await self.release_workspaces_for_agent(agent_id, conn=c)
                await c.execute(
                    update(agents)
                    .where(agents.c.id == agent_id)
                    .values(state=AgentState.RETIRED.value, current_task_id=None)
                )
            return out

        if conn is not None:
            return await _run(conn)
        async with self.immediate() as c:
            out = await _run(c)
        await self._after_release(out)
        return out

    async def lock_filing_scope(self, conn, task_ids: list[str]) -> dict[str, str | None]:
        """Lock the task rows a worker filing's scope is derived from.

        Returns ``{id: parent_task_id}`` for the rows that exist (a missing
        id is simply absent). On Postgres the filing first takes the same
        project-scoped advisory transaction lock as ``set_parent``,
        serializing scope reads with every hierarchy move without contending
        on ordinary project-row updates. It then row-locks the requested
        tasks in ascending id order so deletion cannot invalidate the result.
        The shared hierarchy lock covers intermediate ancestors: moving one
        waits even though it is not itself named by the filing. On SQLite
        ``immediate()`` already holds the database write lock.

        Called first in the filing transaction, before ``reserve_filing``
        takes the same row lock on the held task by writing to it.
        """
        anchor_id = task_ids[0] if task_ids else None
        ids = sorted(set(task_ids))
        if not ids:
            return {}
        project_id = await conn.scalar(select(tasks.c.project_id).where(tasks.c.id == anchor_id))
        if project_id is None:
            return {}
        await self.lock_hierarchy_project(conn, project_id)
        stmt = (
            select(tasks.c.id, tasks.c.parent_task_id)
            .where(tasks.c.id.in_(ids))
            .order_by(tasks.c.id)
            .with_for_update()
        )
        rows = (await conn.execute(stmt)).fetchall()
        return {r.id: r.parent_task_id for r in rows if r.id in ids}

    async def reserve_filing(self, conn, task_id: str, *, max_filings: int) -> bool:
        res = await conn.execute(
            update(tasks)
            .where(and_(tasks.c.id == task_id, tasks.c.filed_count < max_filings))
            .values(filed_count=tasks.c.filed_count + 1)
        )
        return res.rowcount == 1

    async def count_ready_by_profile(self, project_id: str) -> dict[str | None, int]:
        stmt = (
            select(tasks.c.profile_id, func.count())
            .where(_frontier_where(project_id))
            .group_by(tasks.c.profile_id)
        )
        stmt = apply_label_filters(stmt, exclude_hold=True)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return {pid: int(n) for pid, n in rows}
