"""Agent CRUD and status operations."""

from __future__ import annotations

import time

from sqlalchemy import delete, insert, select, update

from src.database.tables import agents, task_results, tasks, token_ledger, workspaces
from src.models import Agent, AgentState


class AgentQueryMixin:
    """Query mixin for agent operations.  Expects ``self._engine``."""

    async def create_agent(self, agent: Agent) -> None:
        """Insert a new agent record."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(agents).values(
                    id=agent.id,
                    name=agent.name,
                    agent_type=agent.agent_type,
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

    async def delete_agent(self, agent_id: str) -> None:
        """Delete an agent and all dependent records.

        Cascading order:
        1. token_ledger rows
        2. task_results rows
        3. workspace locks (release, don't delete)
        4. tasks.assigned_agent_id (NULLify)
        5. agent record
        """
        async with self._engine.begin() as conn:
            await conn.execute(delete(token_ledger).where(token_ledger.c.agent_id == agent_id))
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
            agent_type=row["agent_type"],
            state=AgentState(row["state"]),
            current_task_id=row["current_task_id"],
            pid=row["pid"],
            last_heartbeat=row["last_heartbeat"],
            total_tokens_used=row["total_tokens_used"],
            session_tokens_used=row["session_tokens_used"],
        )
