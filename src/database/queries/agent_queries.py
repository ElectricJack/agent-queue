"""Agent CRUD and status operations."""

from __future__ import annotations

import time

from sqlalchemy import and_, delete, func, insert, or_, select, update

from src.database.tables import agents, events, sessions, task_results, tasks, workspaces
from src.models import Agent, AgentState, TaskStatus


class AgentQueryMixin:
    """Query mixin for agent operations.  Expects ``self._engine``."""

    async def create_agent(self, agent: Agent) -> None:
        """Insert an explicitly requested definition, even after manual deletion."""
        async with self._engine.begin() as conn:
            await self._insert_agent_on(conn, agent)

    async def _insert_agent_on(self, conn, agent: Agent) -> None:
        await conn.execute(
            insert(agents).values(
                id=agent.id,
                name=agent.name,
                profile_id=agent.profile_id,
                role=agent.role,
                enabled=agent.enabled,
                harness=agent.harness,
                model=agent.model,
                intelligence_class=agent.intelligence_class,
                deleted_at=agent.deleted_at,
                state=agent.state.value,
                current_task_id=agent.current_task_id,
                pid=agent.pid,
                last_heartbeat=agent.last_heartbeat,
                total_tokens_used=agent.total_tokens_used,
                session_tokens_used=agent.session_tokens_used,
                created_at=time.time(),
            )
        )

    async def _lock_agent_roster_on(self, conn) -> None:
        """Serialize automatic growth with deletion; SQLite uses BEGIN IMMEDIATE."""
        if conn.dialect.name == "postgresql":
            # Namespace AQFL (agent flock), shared only by these two mutations.
            await conn.execute(select(func.pg_advisory_xact_lock(0x4151464C, 0)))

    async def create_automatic_agent(self, agent: Agent) -> bool:
        """Bootstrap capacity only until the user manually sizes the roster.

        Any worker tombstone records that decision across restarts and profile
        changes. Existing definitions remain reusable; explicit Add Agent is
        unaffected. Check and insertion share the deletion transaction fence.
        """
        async with self.immediate() as conn:
            await self._lock_agent_roster_on(conn)
            deleted = await conn.scalar(
                select(agents.c.id)
                .where(agents.c.role == "worker", agents.c.deleted_at.is_not(None))
                .limit(1)
            )
            if deleted is not None:
                return False
            await self._insert_agent_on(conn, agent)
            return True

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Fetch a single agent by ID."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(agents).where(agents.c.id == agent_id))
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_agent(row)

    async def list_agents(
        self,
        state: AgentState | None = None,
        *,
        include_deleted: bool = False,
    ) -> list[Agent]:
        """List agents, optionally filtered by state."""
        stmt = select(agents)
        if not include_deleted:
            stmt = stmt.where(agents.c.deleted_at.is_(None))
        if state:
            stmt = stmt.where(agents.c.state == state.value)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_agent(r) for r in result.mappings().fetchall()]

    async def update_agent(self, agent_id: str, **kwargs) -> None:
        """Update an existing definition; tombstoned definitions are immutable."""
        if "deleted_at" in kwargs:
            raise ValueError("deleted_at is managed by soft_delete_agent")
        values = {}
        for key, value in kwargs.items():
            if isinstance(value, AgentState):
                value = value.value
            values[key] = value
        async with self._engine.begin() as conn:
            await conn.execute(
                update(agents)
                .where(agents.c.id == agent_id, agents.c.deleted_at.is_(None))
                .values(**values)
            )

    async def soft_delete_agent(self, agent_id: str) -> bool:
        """Hide one idle, unowned worker while retaining all execution history.

        Returns False for missing/already deleted/protected/busy definitions.
        No process is stopped and no assignment or workspace is released.
        """
        active_assignment = (
            select(tasks.c.id)
            .where(
                tasks.c.assigned_agent_id == agent_id,
                tasks.c.status.in_(
                    (
                        TaskStatus.ASSIGNED.value,
                        TaskStatus.IN_PROGRESS.value,
                        TaskStatus.WAITING_INPUT.value,
                    )
                ),
            )
            .exists()
        )
        # Before explicit session links, task ownership was the only link.
        legacy_tasks = select(tasks.c.id).where(tasks.c.assigned_agent_id == agent_id)
        live_session = (
            select(sessions.c.id)
            .where(
                sessions.c.state.in_(("starting", "running", "draining")),
                or_(
                    sessions.c.agent_id == agent_id,
                    and_(sessions.c.agent_id.is_(None), sessions.c.task_id.in_(legacy_tasks)),
                ),
            )
            .exists()
        )
        held_workspace = (
            select(workspaces.c.id).where(workspaces.c.locked_by_agent_id == agent_id).exists()
        )
        async with self.immediate() as conn:
            await self._lock_agent_roster_on(conn)
            result = await conn.execute(
                update(agents)
                .where(
                    agents.c.id == agent_id,
                    agents.c.id != "supervisor-global",
                    agents.c.role == "worker",
                    agents.c.state == AgentState.IDLE.value,
                    agents.c.current_task_id.is_(None),
                    agents.c.deleted_at.is_(None),
                    ~active_assignment,
                    ~live_session,
                    ~held_workspace,
                )
                .values(enabled=False, deleted_at=time.time())
            )
            return result.rowcount == 1

    async def release_agent_for_task(self, agent_id: str, task_id: str) -> bool:
        """Release only the assignment owned by this completing task."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(agents)
                .where(
                    agents.c.id == agent_id,
                    agents.c.current_task_id == task_id,
                )
                .values(state=AgentState.IDLE.value, current_task_id=None)
            )
            return result.rowcount == 1

    async def _reserve_idle_agent_on(self, conn, agent_id: str) -> bool:
        active_assignment = select(tasks.c.id).where(
            tasks.c.assigned_agent_id == agent_id,
            tasks.c.status.in_((
                TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value,
                TaskStatus.WAITING_INPUT.value,
            )),
        ).exists()
        held_workspace = select(workspaces.c.id).where(
            workspaces.c.locked_by_agent_id == agent_id,
        ).exists()
        live = (
            select(sessions.c.id)
            .where(
                sessions.c.agent_id == agent_id,
                sessions.c.state.in_(("starting", "running", "draining")),
            )
            .exists()
        )
        result = await conn.execute(
            update(agents)
            .where(
                agents.c.id == agent_id,
                agents.c.state == AgentState.IDLE.value,
                agents.c.current_task_id.is_(None),
                agents.c.enabled.is_(True),
                agents.c.deleted_at.is_(None),
                agents.c.role == "worker",
                ~live,
                ~active_assignment,
                ~held_workspace,
            )
            .values(state=AgentState.BUSY.value, last_heartbeat=time.time())
        )
        return result.rowcount == 1

    async def reserve_idle_agent(self, agent_id: str) -> bool:
        """Reserve a global identity while its next session is being launched."""
        async with self.immediate() as conn:
            return await self._reserve_idle_agent_on(conn, agent_id)

    async def release_agent_reservation(
        self, agent_id: str, *, expected_heartbeat: float | None
    ) -> bool:
        """Release this launch reservation only, never a successor or live owner."""
        live = select(sessions.c.id).where(
            sessions.c.agent_id == agent_id,
            sessions.c.state.in_(("starting", "running", "draining")),
        ).exists()
        async with self.immediate() as conn:
            result = await conn.execute(
                update(agents).where(
                    agents.c.id == agent_id, agents.c.state == AgentState.BUSY.value,
                    agents.c.current_task_id.is_(None), agents.c.deleted_at.is_(None),
                    agents.c.last_heartbeat == expected_heartbeat, ~live,
                ).values(state=AgentState.IDLE.value)
            )
            return result.rowcount == 1

    async def _assign_task_to_agent(self, task_id: str, agent_id: str) -> bool:
        """One conditional transaction binds a ready task and available worker."""

        class AssignmentConflict(Exception):
            pass

        try:
            async with self.immediate() as conn:
                if not await self._reserve_idle_agent_on(conn, agent_id):
                    return False
                # READY can retain historical assigned_agent_id (for example
                # a human reply). Only active execution, not that stale label,
                # blocks a fresh assignment.
                live_task = (
                    select(sessions.c.id)
                    .where(
                        sessions.c.task_id == task_id,
                        sessions.c.state.in_(("starting", "running", "draining")),
                    )
                    .exists()
                )
                busy_owner = (
                    select(agents.c.id)
                    .where(
                        agents.c.current_task_id == task_id,
                        agents.c.state == AgentState.BUSY.value,
                        agents.c.id != agent_id,
                    )
                    .exists()
                )
                result = await conn.execute(
                    update(tasks)
                    .where(
                        tasks.c.id == task_id,
                        tasks.c.status == TaskStatus.READY.value,
                        tasks.c.is_blocked == 0,
                        ~live_task,
                        ~busy_owner,
                    )
                    .values(
                        status=TaskStatus.ASSIGNED.value,
                        assigned_agent_id=agent_id,
                        updated_at=time.time(),
                    )
                )
                if result.rowcount != 1:
                    raise AssignmentConflict
                await conn.execute(
                    update(agents).where(agents.c.id == agent_id).values(current_task_id=task_id)
                )
                await conn.execute(
                    insert(events).values(
                        event_type="task_assigned",
                        project_id=select(tasks.c.project_id)
                        .where(tasks.c.id == task_id)
                        .scalar_subquery(),
                        task_id=task_id,
                        agent_id=agent_id,
                        timestamp=time.time(),
                    )
                )
                await self.recompute_blocked({task_id}, conn=conn)
        except AssignmentConflict:
            return False
        return True

    async def delete_agent(self, agent_id: str) -> None:
        """Delete an agent and all dependent records.

        Cascading order:
        1. task_results rows
        2. workspace locks (release, don't delete)
        3. tasks.assigned_agent_id (NULLify)
        4. agent record

        Token-ledger rows retain their historical attribution if an operator
        explicitly deletes a definition. Automatic lifecycle management never
        deletes durable workers.
        """
        async with self._engine.begin() as conn:
            await conn.execute(delete(task_results).where(task_results.c.agent_id == agent_id))
            await conn.execute(
                update(workspaces)
                .where(workspaces.c.locked_by_agent_id == agent_id)
                .values(locked_by_agent_id=None, locked_by_task_id=None, locked_at=None)
            )
            await conn.execute(
                update(tasks)
                .where(tasks.c.assigned_agent_id == agent_id)
                .values(assigned_agent_id=None)
            )
            await conn.execute(delete(agents).where(agents.c.id == agent_id))

    @staticmethod
    def _row_to_agent(row) -> Agent:
        """Convert a database row to an Agent model."""
        return Agent(
            id=row["id"],
            name=row["name"],
            profile_id=row["profile_id"],
            role=row.get("role", "worker"),
            enabled=bool(row.get("enabled", True)),
            harness=row.get("harness"),
            model=row.get("model"),
            intelligence_class=row.get("intelligence_class"),
            deleted_at=row.get("deleted_at"),
            state=AgentState(row["state"]),
            current_task_id=row["current_task_id"],
            pid=row["pid"],
            last_heartbeat=row["last_heartbeat"],
            total_tokens_used=row["total_tokens_used"],
            session_tokens_used=row["session_tokens_used"],
            created_at=row.get("created_at", 0.0) or 0.0,
        )
