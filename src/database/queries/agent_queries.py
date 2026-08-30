"""Agent CRUD and status operations."""

from __future__ import annotations

import time

from sqlalchemy import delete, insert, select, update

from src.database.tables import agents, events, sessions, task_results, tasks, workspaces
from src.models import Agent, AgentState, TaskStatus


class AgentQueryMixin:
    """Query mixin for agent operations.  Expects ``self._engine``."""

    async def create_agent(self, agent: Agent) -> None:
        """Insert a new agent record."""
        async with self._engine.begin() as conn:
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
                    state=agent.state.value,
                    current_task_id=agent.current_task_id,
                    pid=agent.pid,
                    last_heartbeat=agent.last_heartbeat,
                    total_tokens_used=agent.total_tokens_used,
                    session_tokens_used=agent.session_tokens_used,
                    created_at=time.time(),
                )
            )

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
    ) -> list[Agent]:
        """List agents, optionally filtered by state."""
        stmt = select(agents)
        if state:
            stmt = stmt.where(agents.c.state == state.value)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_agent(r) for r in result.mappings().fetchall()]

    async def update_agent(self, agent_id: str, **kwargs) -> None:
        """Update arbitrary agent fields."""
        values = {}
        for key, value in kwargs.items():
            if isinstance(value, AgentState):
                value = value.value
            values[key] = value
        async with self._engine.begin() as conn:
            await conn.execute(update(agents).where(agents.c.id == agent_id).values(**values))

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
                agents.c.role == "worker",
                ~live,
            )
            .values(state=AgentState.BUSY.value, last_heartbeat=time.time())
        )
        return result.rowcount == 1

    async def reserve_idle_agent(self, agent_id: str) -> bool:
        """Reserve a global identity while its next session is being launched."""
        async with self.immediate() as conn:
            return await self._reserve_idle_agent_on(conn, agent_id)

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
            state=AgentState(row["state"]),
            current_task_id=row["current_task_id"],
            pid=row["pid"],
            last_heartbeat=row["last_heartbeat"],
            total_tokens_used=row["total_tokens_used"],
            session_tokens_used=row["session_tokens_used"],
            created_at=row.get("created_at", 0.0) or 0.0,
        )
