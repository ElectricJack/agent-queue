"""Agent CRUD and status operations."""

from __future__ import annotations

import time

from sqlalchemy import delete, insert, select, update

from src.database.tables import agents, task_results, tasks, workspaces
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
                    profile_id=agent.profile_id,
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
        1. task_results rows
        2. workspace locks (release, don't delete)
        3. tasks.assigned_agent_id (NULLify)
        4. agent record

        ``token_ledger`` rows are intentionally left behind.  Agents are
        ephemeral — the startup reconciler reaps any whose profile no longer
        resolves, plus any idle agents over a project's concurrency cap — and
        cascading into the ledger meant each reap silently destroyed that
        agent's entire spend history.  ``agent_id`` survives as a best-effort
        attribution string; ``get_cost_rollup`` already outer-joins ``agents``
        so unresolvable ids roll up under "(unknown)".
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
            state=AgentState(row["state"]),
            current_task_id=row["current_task_id"],
            pid=row["pid"],
            last_heartbeat=row["last_heartbeat"],
            total_tokens_used=row["total_tokens_used"],
            session_tokens_used=row["session_tokens_used"],
            created_at=row.get("created_at", 0.0) or 0.0,
        )
