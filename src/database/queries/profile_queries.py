"""Agent profile CRUD operations."""

from __future__ import annotations

import json
import time

from src.models import AgentProfile


class ProfileQueryMixin:
    """Query mixin for agent profile operations.  Expects ``self._db``."""

    async def create_profile(self, profile: AgentProfile) -> None:
        """Insert a new agent profile."""
        now = time.time()
        await self._db.execute(
            "INSERT INTO agent_profiles (id, name, description, model, "
            "permission_mode, allowed_tools, mcp_servers, "
            "system_prompt_suffix, install, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                profile.id,
                profile.name,
                profile.description,
                profile.model,
                profile.permission_mode,
                json.dumps(profile.allowed_tools),
                json.dumps(profile.mcp_servers),
                profile.system_prompt_suffix,
                json.dumps(profile.install),
                now,
                now,
            ),
        )
        await self._db.commit()

    async def get_profile(self, profile_id: str) -> AgentProfile | None:
        """Fetch a single profile by ID."""
        cursor = await self._db.execute("SELECT * FROM agent_profiles WHERE id = ?", (profile_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_profile(row)

    async def list_profiles(self) -> list[AgentProfile]:
        """List all agent profiles ordered by name."""
        cursor = await self._db.execute("SELECT * FROM agent_profiles ORDER BY name ASC")
        rows = await cursor.fetchall()
        return [self._row_to_profile(r) for r in rows]

    async def update_profile(self, profile_id: str, **kwargs) -> None:
        """Update arbitrary profile fields."""
        sets = []
        vals = []
        for key, value in kwargs.items():
            if key in ("allowed_tools", "mcp_servers", "install"):
                value = json.dumps(value)
            sets.append(f"{key} = ?")
            vals.append(value)
        sets.append("updated_at = ?")
        vals.append(time.time())
        vals.append(profile_id)
        await self._db.execute(f"UPDATE agent_profiles SET {', '.join(sets)} WHERE id = ?", vals)
        await self._db.commit()

    async def delete_profile(self, profile_id: str) -> None:
        """Delete a profile and clear foreign-key references."""
        await self._db.execute(
            "UPDATE tasks SET profile_id = NULL WHERE profile_id = ?",
            (profile_id,),
        )
        await self._db.execute(
            "UPDATE projects SET default_profile_id = NULL WHERE default_profile_id = ?",
            (profile_id,),
        )
        await self._db.execute("DELETE FROM agent_profiles WHERE id = ?", (profile_id,))
        await self._db.commit()

    @staticmethod
    def _row_to_profile(row) -> AgentProfile:
        """Convert a database row to an AgentProfile model."""
        return AgentProfile(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            model=row["model"],
            permission_mode=row["permission_mode"],
            allowed_tools=json.loads(row["allowed_tools"]),
            mcp_servers=json.loads(row["mcp_servers"]),
            system_prompt_suffix=row["system_prompt_suffix"],
            install=json.loads(row["install"]),
        )
