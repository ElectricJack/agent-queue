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
from src.database.queries.hierarchy_queries import container_flag_exists
from src.database.queries.session_queries import _row_to_session
from src.database.queries.task_queries import (
    ManualPauseActive, TransitionResult, _not_manually_paused, supports_returning,
)
from src.database.tables import (
    agents,
    sessions,
    task_assignment_routes,
    task_metadata,
    task_workspace_requirements,
    tasks,
    workspaces,
)
from src.models import AgentState, SessionRecord, Task, TaskEvent, TaskStatus, Workspace


def _frontier_where(project_id: str):
    return and_(
        tasks.c.project_id == project_id,
        tasks.c.status == TaskStatus.READY.value,
        tasks.c.is_blocked == 0,
        tasks.c.assigned_agent_id.is_(None),
        tasks.c.is_plan_subtask == 0,
        # A flagged container (spec §7) has no deliverable of its own: it is
        # released to IN_PROGRESS by the orchestrator and settles when its
        # children finish.  A worker holding it could never close it
        # (Invariant 6) and its live session would block the settlement it
        # waits for (calm-ember-48).
        ~container_flag_exists(),
    )


# Kept in task metadata rather than a task column: this is operational
# claim-state, not lifecycle state, and therefore needs no schema migration.
PREPARE_BACKOFF_UNTIL_KEY = "claim_prepare_backoff_until"
PREPARE_BACKOFF_ATTEMPTS_KEY = "claim_prepare_backoff_attempts"
PREPARE_BACKOFF_INITIAL_SECONDS = 120.0
PREPARE_BACKOFF_MAX_SECONDS = 300.0


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
        self, session_id: str, task_id: str, claim_epoch: int
    ) -> bool:
        """Verify the task and pool-session fences before filesystem work.

        Slot reset and claim-file creation happen outside the claim
        transaction, so this read must prove both rows are still current.
        Joining their two predicates avoids two independent round trips on
        every successful pool claim.
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
        async with self._engine.connect() as conn:
            return (await conn.execute(stmt)).scalar_one_or_none() is not None

    async def release_claim_slot(self, conn, session_id: str) -> None:
        await conn.execute(
            update(sessions)
            .where(and_(sessions.c.id == session_id, sessions.c.claim_phase == "claiming"))
            .values(claim_phase=None, claim_phase_at=None)
        )

    async def select_ready_for_profile(
        self, conn, *, project_id, profile_id, default_profile_id, agent_id, task_id=None,
        enforce_routing=False, intelligence_class=None, llm_provider=None, options_hash=None,
    ) -> str | None:
        """The §10 work query.  Postgres takes the row FOR UPDATE SKIP LOCKED."""
        profile_ok = tasks.c.profile_id == profile_id
        if default_profile_id == profile_id:
            profile_ok = (tasks.c.profile_id == profile_id) | tasks.c.profile_id.is_(None)
        req = task_workspace_requirements.alias("req")
        prepare_backoff_active = exists(
            select(literal(1)).where(
                task_metadata.c.task_id == tasks.c.id,
                task_metadata.c.key == PREPARE_BACKOFF_UNTIL_KEY,
                cast(task_metadata.c.value, Float) > time.time(),
            )
        )
        stmt = (
            select(tasks.c.id)
            .where(
                _frontier_where(project_id),
                profile_ok,
                ~exists(
                    select(literal(1)).where(
                        and_(req.c.task_id == tasks.c.id, req.c.kind_id != "project-repo")
                    )
                ),
                ~prepare_backoff_active,
            )
            .order_by(
                case((tasks.c.affinity_agent_id == agent_id, 0), else_=1),
                tasks.c.priority.asc(),
                tasks.c.created_at.asc(),
            )
            .limit(1)
        )
        stmt = apply_label_filters(stmt, exclude_hold=True)
        if enforce_routing:
            # Join the persisted playbook decision into the locking query so
            # selection and claim cannot race an intervening task edit. An
            # explicit task class wins and does not inherit a provider pin.
            explicit_class = func.nullif(func.trim(tasks.c.intelligence_class), "")
            saved_route_is_fresh = and_(
                task_assignment_routes.c.task_id == tasks.c.id,
                task_assignment_routes.c.project_id == tasks.c.project_id,
                task_assignment_routes.c.task_updated_at == tasks.c.updated_at,
                task_assignment_routes.c.options_hash == options_hash,
            )
            routed_class = case(
                (saved_route_is_fresh, task_assignment_routes.c.intelligence_class),
                else_=None,
            )
            effective_class = func.coalesce(explicit_class, routed_class)
            effective_provider = case(
                (explicit_class.is_not(None), None),
                (saved_route_is_fresh, task_assignment_routes.c.provider),
                else_=None,
            )
            provider_ok = effective_provider.is_(None)
            if llm_provider:
                provider_ok = provider_ok | (effective_provider == llm_provider)
            stmt = stmt.select_from(
                tasks.outerjoin(
                    task_assignment_routes,
                    task_assignment_routes.c.task_id == tasks.c.id,
                )
            ).where(
                effective_class == intelligence_class if intelligence_class else false(),
                provider_ok,
            )
        if task_id is not None:
            stmt = stmt.where(tasks.c.id == task_id)
        if conn.dialect.name == "postgresql":
            # The routing gate adds a LEFT OUTER JOIN.  PostgreSQL cannot
            # lock the nullable (route) side of that join, so explicitly
            # lock only the task row that this query is selecting to claim.
            stmt = stmt.with_for_update(of=tasks, skip_locked=True)
        row = (await conn.execute(stmt)).fetchone()
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
            reserved = (await conn.execute(reserve.returning(agents.c.id))).scalar_one_or_none() is not None
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
            stmt = (
                update(workspaces)
                .where(workspaces.c.locked_by_agent_id == agent_id)
                .values(locked_by_task_id=task_id)
            )
            if supports_returning(conn):
                row = (await conn.execute(stmt.returning(*workspaces.c))).mappings().fetchone()
                slot = self._row_to_workspace(row) if row is not None else None
            else:
                await conn.execute(stmt)
                row = (
                    (
                        await conn.execute(
                            select(workspaces).where(workspaces.c.locked_by_agent_id == agent_id)
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
            conn, session_id, started_at=now, work_dir=work_dir,
        )
        return slot

    async def activate_claim(
        self, session_id, task_id, *, epoch: int, now: float, conn=None
    ) -> SessionRecord | None:
        """Flip ``preparing`` -> ``active``; the updated row, or ``None``.

        Returning the row (via ``RETURNING`` where the dialect has it) saves
        the caller a re-read to build the response's session block.  Falsy
        on failure, so the old ``if not await activate_claim(...)`` callers
        read unchanged.
        """

        async def _run(c):
            # Claims and release acquire the session before the task. Keep
            # activation in that order as well to avoid a PostgreSQL deadlock.
            holder = (await c.execute(select(sessions.c.id).where(
                sessions.c.id == session_id,
            ).with_for_update())).scalar_one_or_none()
            if holder is None:
                return None
            # Share the task row lock with pause. An EXISTS predicate alone
            # can observe a pre-pause PostgreSQL statement snapshot.
            claim = (await c.execute(select(tasks.c.id).where(
                tasks.c.id == task_id, tasks.c.status == TaskStatus.IN_PROGRESS.value,
                tasks.c.claim_epoch == epoch,
            ).with_for_update())).scalar_one_or_none()
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
            # shadows a live task in the dashboard or recovery logic.
            await c.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == task_id,
                    task_metadata.c.key == "needs_attention",
                )
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
        drain_after_release=False,
        end_reason=None,
    ) -> TransitionResult:
        row = (
            (await conn.execute(select(sessions).where(sessions.c.id == session_id).with_for_update()))
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
        epoch = None
        if task_id:
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
            )
            if task_status == TaskStatus.READY:
                # An active claim can only release the IN_PROGRESS,
                # unblocked task it holds.  Put that proof in the UPDATE
                # itself so _apply_transition may skip its validation read;
                # a concurrent close/pause simply leaves this task unchanged.
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
                session_id, task_id=task_id, ended_at=now,
                end_reason=end_reason or needs_attention or context, conn=conn,
            )
        if agent_id:
            # Clear the task lock unconditionally — even a session that held no
            # task (e.g. released mid-``claiming``) must not leave a stale
            # ``locked_by_task_id`` on its agent's workspace.  The agent lock
            # itself (``locked_by_agent_id``) is retained; only
            # ``terminate_pool_session`` releases it.
            await conn.execute(
                update(workspaces)
                .where(workspaces.c.locked_by_agent_id == agent_id)
                .values(locked_by_task_id=None)
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
        await conn.execute(
            update(sessions)
            .where(sessions.c.id == session_id)
            .values(**session_values)
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
        drain_after_release=False,
        prepare_backoff=False,
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
            drain_after_release=drain_after_release,
            prepare_backoff=prepare_backoff,
        )
        if conn is not None:
            return await self._release_claim_on(conn, session_id, **kwargs)
        async with self.immediate() as conn:
            out = await self._release_claim_on(conn, session_id, **kwargs)
        await self._after_release(out)
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
