"""Agent CRUD and status operations."""

from __future__ import annotations

import time

from src.models import Agent, AgentState


class AgentQueryMixin:
    """Query mixin for agent operations.  Expects ``self._db``."""

    async def create_agent(self, agent: Agent) -> None:
        """Insert a new agent record."""
        await self._db.execute(
            "INSERT INTO agents (id, name, agent_type, state, current_task_id, "
            "pid, last_heartbeat, total_tokens_used, "
            "session_tokens_used, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent.id, agent.name, agent.agent_type,
             agent.state.value, agent.current_task_id,
             agent.pid, agent.last_heartbeat,
             agent.total_tokens_used, agent.session_tokens_used, time.time()),
        )
        await self._db.commit()

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Fetch a single agent by ID."""
        cursor = await self._db.execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_agent(row)

    async def list_agents(
        self, state: AgentState | None = None,
    ) -> list[Agent]:
        """List agents, optionally filtered by state."""
        if state:
            cursor = await self._db.execute(
                "SELECT * FROM agents WHERE state = ?", (state.value,)
            )
        else:
            cursor = await self._db.execute("SELECT * FROM agents")
        rows = await cursor.fetchall()
        return [self._row_to_agent(r) for r in rows]

    async def update_agent(self, agent_id: str, **kwargs) -> None:
        """Update arbitrary agent fields."""
        sets = []
        vals = []
        for key, value in kwargs.items():
            if isinstance(value, AgentState):
                value = value.value
            sets.append(f"{key} = ?")
            vals.append(value)
        vals.append(agent_id)
        await self._db.execute(
            f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self._db.commit()

    async def delete_agent(self, agent_id: str) -> None:
        """Delete an agent and all dependent records.

        Cascading order:
        1. token_ledger rows
        2. task_results rows
        3. workspace locks (release, don't delete)
        4. tasks.assigned_agent_id (NULLify)
        5. agent record
        """
        await self._db.execute(
            "DELETE FROM token_ledger WHERE agent_id = ?", (agent_id,),
        )
        await self._db.execute(
            "DELETE FROM task_results WHERE agent_id = ?", (agent_id,),
        )
        await self._db.execute(
            "UPDATE workspaces SET locked_by_agent_id = NULL, "
            "locked_by_task_id = NULL, locked_at = NULL "
            "WHERE locked_by_agent_id = ?",
            (agent_id,),
        )
        await self._db.execute(
            "UPDATE tasks SET assigned_agent_id = NULL WHERE assigned_agent_id = ?",
            (agent_id,),
        )
        await self._db.execute(
            "DELETE FROM agents WHERE id = ?", (agent_id,),
        )
        await self._db.commit()

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
