"""Claims — who holds what (swarm-work-model §10, §11.2).

Every write that records a holder goes through this mixin on a caller-owned
connection opened by ``immediate()``: the session slot is taken with a
conditional UPDATE first (row lock on Postgres, writer lock on SQLite), then
the task, then agent/workspace/metadata.  ``activate_claim`` and
``release_claim`` are the two ways a claim leaves ``preparing`` and they
race by design — the conditional UPDATEs make exactly one of them win.
"""

from __future__ import annotations

import time

from sqlalchemy import and_, case, exists, func, literal, select, update

from src.database.queries.blocked_state import apply_label_filters
from src.database.queries.session_queries import _row_to_session
from src.database.queries.task_queries import TransitionResult
from src.database.tables import agents, sessions, task_workspace_requirements, tasks, workspaces
from src.models import AgentState, Task, TaskEvent, TaskStatus


def _frontier_where(project_id: str):
    return and_(
        tasks.c.project_id == project_id,
        tasks.c.status == TaskStatus.READY.value,
        tasks.c.is_blocked == 0,
        tasks.c.assigned_agent_id.is_(None),
        tasks.c.is_plan_subtask == 0,
    )


class ClaimQueryMixin:
    """Expects ``self._engine`` plus Task/Session/Workspace/Hierarchy mixins."""

    async def take_claim_slot(self, conn, session_id: str, *, now: float, cap: int | None):
        cond = [
            sessions.c.id == session_id,
            sessions.c.task_id.is_(None),
            sessions.c.claim_phase.is_(None),
            sessions.c.desired_state == "running",
        ]
        if cap is not None:
            cond.append(sessions.c.claims < cap)
        res = await conn.execute(
            update(sessions).where(and_(*cond)).values(claim_phase="claiming", claim_phase_at=now)
        )
        row = (
            (await conn.execute(select(sessions).where(sessions.c.id == session_id)))
            .mappings()
            .fetchone()
        )
        if row is None:
            return "not_found", None
        record = _row_to_session(row)
        if res.rowcount == 1:
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

    async def release_claim_slot(self, conn, session_id: str) -> None:
        await conn.execute(
            update(sessions)
            .where(and_(sessions.c.id == session_id, sessions.c.claim_phase == "claiming"))
            .values(claim_phase=None, claim_phase_at=None)
        )

    async def select_ready_for_profile(
        self, conn, *, project_id, profile_id, default_profile_id, agent_id, task_id=None
    ) -> str | None:
        """The §10 work query.  Postgres takes the row FOR UPDATE SKIP LOCKED."""
        profile_ok = tasks.c.profile_id == profile_id
        if default_profile_id == profile_id:
            profile_ok = (tasks.c.profile_id == profile_id) | tasks.c.profile_id.is_(None)
        req = task_workspace_requirements.alias("req")
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
            )
            .order_by(
                case((tasks.c.affinity_agent_id == agent_id, 0), else_=1),
                tasks.c.priority.asc(),
                tasks.c.created_at.asc(),
            )
            .limit(1)
        )
        stmt = apply_label_filters(stmt, exclude_hold=True)
        if task_id is not None:
            stmt = stmt.where(tasks.c.id == task_id)
        if conn.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        row = (await conn.execute(stmt)).fetchone()
        return row[0] if row else None

    async def take_task(self, conn, task_id: str, *, agent_id: str, now: float) -> Task | None:
        res = await conn.execute(
            update(tasks)
            .where(
                and_(
                    tasks.c.id == task_id,
                    tasks.c.status == TaskStatus.READY.value,
                    tasks.c.is_blocked == 0,
                    tasks.c.assigned_agent_id.is_(None),
                )
            )
            .values(claim_epoch=tasks.c.claim_epoch + 1)
        )
        if res.rowcount != 1:
            return None
        await self._apply_transition(
            conn,
            task_id,
            TaskStatus.IN_PROGRESS,
            context="claim",
            event=TaskEvent.CLAIMED,
            assigned_agent_id=agent_id,
        )
        row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().fetchone()
        return self._row_to_task(row)

    async def bump_claim_epoch(self, task_id: str, *, conn=None) -> int:
        async def _run(c):
            await c.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(claim_epoch=tasks.c.claim_epoch + 1)
            )
            return (
                await c.execute(select(tasks.c.claim_epoch).where(tasks.c.id == task_id))
            ).scalar() or 0

        if conn is not None:
            return await _run(conn)
        async with self.immediate() as conn:
            return await _run(conn)

    async def record_holder(self, conn, *, session_id, task_id, agent_id, work_dir, now) -> None:
        await conn.execute(
            update(sessions)
            .where(sessions.c.id == session_id)
            .values(task_id=task_id, claim_phase="preparing", claim_phase_at=now)
        )
        if agent_id:
            await conn.execute(
                update(agents)
                .where(agents.c.id == agent_id)
                .values(state=AgentState.BUSY.value, current_task_id=task_id)
            )
            await conn.execute(
                update(workspaces)
                .where(workspaces.c.locked_by_agent_id == agent_id)
                .values(locked_by_task_id=task_id)
            )
        await self._upsert_meta(task_id, "claimed_by_session", session_id, conn=conn)
        await self._upsert_meta(task_id, "work_dir", work_dir, conn=conn)

    async def activate_claim(
        self, session_id, task_id, *, epoch: int, now: float, conn=None
    ) -> bool:
        async def _run(c):
            res = await c.execute(
                update(sessions)
                .where(
                    and_(
                        sessions.c.id == session_id,
                        sessions.c.claim_phase == "preparing",
                        sessions.c.task_id == task_id,
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
            return res.rowcount == 1

        if conn is not None:
            return await _run(conn)
        async with self.immediate() as conn:
            return await _run(conn)

    async def _release_claim_on(
        self, conn, session_id, *, task_status, context, now, result, needs_attention
    ) -> TransitionResult:
        row = (
            (await conn.execute(select(sessions).where(sessions.c.id == session_id)))
            .mappings()
            .fetchone()
        )
        out = TransitionResult()
        if row is None:
            return out
        task_id, agent_id = row["task_id"], row["agent_id"]
        epoch = None
        if task_id:
            epoch = (
                await conn.execute(select(tasks.c.claim_epoch).where(tasks.c.id == task_id))
            ).scalar()
            out = await self._apply_transition(
                conn, task_id, task_status, context=context, force=True, assigned_agent_id=None
            )
            if needs_attention:
                await self._upsert_meta(task_id, "needs_attention", needs_attention, conn=conn)
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
        await conn.execute(
            update(sessions)
            .where(sessions.c.id == session_id)
            .values(
                task_id=None,
                claim_phase=None,
                claim_phase_at=None,
                last_claim_epoch=epoch,
                last_claim_result=result,
            )
        )
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
        conn=None,
    ) -> TransitionResult:
        kwargs = dict(
            task_status=task_status,
            context=context,
            now=now,
            result=result,
            needs_attention=needs_attention,
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
