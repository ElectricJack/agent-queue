"""Agent profile CRUD operations."""

from __future__ import annotations

import json
import time

from sqlalchemy import delete, insert, select, update

from src.database.tables import agent_profiles, projects, tasks
from src.models import AgentProfile


class ProfileQueryMixin:
    """Query mixin for agent profile operations.  Expects ``self._engine``."""

    async def create_profile(self, profile: AgentProfile) -> None:
        """Insert a new agent profile."""
        now = time.time()
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(agent_profiles).values(
                    id=profile.id,
                    name=profile.name,
                    description=profile.description,
                    model=profile.model,
                    permission_mode=profile.permission_mode,
                    allowed_tools=json.dumps(profile.allowed_tools),
                    mcp_servers=json.dumps(profile.mcp_servers),
                    system_prompt_suffix=profile.system_prompt_suffix,
                    install=json.dumps(profile.install),
                    memory_scope_id=profile.memory_scope_id,
                    created_at=now,
                    updated_at=now,
                )
            )

    async def get_profile(self, profile_id: str) -> AgentProfile | None:
        """Fetch a single profile by ID."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(agent_profiles).where(agent_profiles.c.id == profile_id)
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_profile(row)

    async def list_profiles(self) -> list[AgentProfile]:
        """List all agent profiles ordered by name."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(agent_profiles).order_by(agent_profiles.c.name.asc())
            )
            return [self._row_to_profile(r) for r in result.mappings().fetchall()]

    async def update_profile(self, profile_id: str, **kwargs) -> None:
        """Update arbitrary profile fields."""
        values = {}
        for key, value in kwargs.items():
            if key in ("allowed_tools", "mcp_servers", "install"):
                value = json.dumps(value)
            values[key] = value
        values["updated_at"] = time.time()
        async with self._engine.begin() as conn:
            await conn.execute(
                update(agent_profiles).where(agent_profiles.c.id == profile_id).values(**values)
            )

    async def upsert_profile(self, profile: AgentProfile) -> str:
        """Insert or update an agent profile.

        If a profile with the same ``id`` already exists, all mutable fields
        are updated.  Otherwise a new row is inserted.

        Returns
        -------
        str
            ``"created"`` if a new row was inserted, ``"updated"`` if an
            existing row was modified.
        """
        existing = await self.get_profile(profile.id)
        if existing:
            await self.update_profile(
                profile.id,
                name=profile.name,
                description=profile.description,
                model=profile.model,
                permission_mode=profile.permission_mode,
                allowed_tools=profile.allowed_tools,
                mcp_servers=profile.mcp_servers,
                system_prompt_suffix=profile.system_prompt_suffix,
                install=profile.install,
                memory_scope_id=profile.memory_scope_id,
            )
            return "updated"
        else:
            await self.create_profile(profile)
            return "created"

    async def delete_profile(self, profile_id: str) -> None:
        """Delete a profile and clear foreign-key references."""
        async with self._engine.begin() as conn:
            await conn.execute(
                update(tasks).where(tasks.c.profile_id == profile_id).values(profile_id=None)
            )
            await conn.execute(
                update(projects)
                .where(projects.c.default_profile_id == profile_id)
                .values(default_profile_id=None)
            )
            await conn.execute(delete(agent_profiles).where(agent_profiles.c.id == profile_id))

    @staticmethod
    def _row_to_profile(row) -> AgentProfile:
        """Convert a database row to an AgentProfile model.

        ``mcp_servers`` is normalised to ``list[str]`` (registry names).
        Legacy rows that still hold an inline ``dict[str, dict]`` are
        coerced to a list of keys so the daemon can keep running until
        the inline-config migration writes them out to the registry.
        """
        raw_mcp = json.loads(row["mcp_servers"] or "[]")
        if isinstance(raw_mcp, dict):
            mcp_servers = list(raw_mcp.keys())
        elif isinstance(raw_mcp, list):
            mcp_servers = [str(n) for n in raw_mcp if isinstance(n, str)]
        else:
            mcp_servers = []
        return AgentProfile(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            model=row["model"],
            permission_mode=row["permission_mode"],
            allowed_tools=json.loads(row["allowed_tools"]),
            mcp_servers=mcp_servers,
            system_prompt_suffix=row["system_prompt_suffix"],
            install=json.loads(row["install"]),
            memory_scope_id=row.get("memory_scope_id"),
        )
