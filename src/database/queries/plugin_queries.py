"""Plugin and plugin_data CRUD operations."""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import plugin_data, plugins


class PluginQueryMixin:
    """Query mixin for plugin operations.  Expects ``self._engine``."""

    # --- Plugins ---

    async def create_plugin(
        self,
        *,
        plugin_id: str,
        version: str = "0.0.0",
        source_url: str = "",
        source_rev: str = "",
        source_branch: str = "",
        install_path: str = "",
        status: str = "installed",
        config: str = "{}",
        permissions: str = "[]",
    ) -> None:
        """Insert a new plugin record."""
        now = time.time()
        stmt = sqlite_insert(plugins).values(
            id=plugin_id,
            version=version,
            source_url=source_url,
            source_rev=source_rev,
            source_branch=source_branch,
            install_path=install_path,
            status=status,
            config=config,
            permissions=permissions,
            error_message=None,
            installed_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_nothing()
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def get_plugin(self, plugin_id: str) -> dict | None:
        """Fetch a single plugin by ID."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(plugins).where(plugins.c.id == plugin_id))
            row = result.mappings().fetchone()
            if not row:
                return None
            return dict(row)

    async def list_plugins(
        self,
        status: str | None = None,
    ) -> list[dict]:
        """List plugins with optional status filter."""
        stmt = select(plugins)
        if status:
            stmt = stmt.where(plugins.c.status == status)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [dict(r) for r in result.mappings().fetchall()]

    async def update_plugin(self, plugin_id: str, **kwargs) -> None:
        """Update arbitrary plugin fields."""
        if not kwargs:
            return
        kwargs["updated_at"] = time.time()
        async with self._engine.begin() as conn:
            await conn.execute(update(plugins).where(plugins.c.id == plugin_id).values(**kwargs))

    async def delete_plugin(self, plugin_id: str) -> None:
        """Delete a plugin record."""
        async with self._engine.begin() as conn:
            await conn.execute(delete(plugin_data).where(plugin_data.c.plugin_id == plugin_id))
            await conn.execute(delete(plugins).where(plugins.c.id == plugin_id))

    # --- Plugin Data ---

    async def get_plugin_data(self, plugin_id: str, key: str) -> Any:
        """Fetch a single plugin data value."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(plugin_data.c.value).where(
                    (plugin_data.c.plugin_id == plugin_id) & (plugin_data.c.key == key)
                )
            )
            row = result.fetchone()
            if not row:
                return None
            try:
                return json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                return row[0]

    async def set_plugin_data(self, plugin_id: str, key: str, value: Any) -> None:
        """Insert or update a plugin data value."""
        now = time.time()
        json_value = json.dumps(value)
        stmt = sqlite_insert(plugin_data).values(
            plugin_id=plugin_id,
            key=key,
            value=json_value,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["plugin_id", "key"],
            set_={"value": json_value, "updated_at": now},
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def delete_plugin_data(self, plugin_id: str, key: str) -> None:
        """Delete a single plugin data entry."""
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(plugin_data).where(
                    (plugin_data.c.plugin_id == plugin_id) & (plugin_data.c.key == key)
                )
            )

    async def delete_plugin_data_all(self, plugin_id: str) -> None:
        """Delete all data for a plugin."""
        async with self._engine.begin() as conn:
            await conn.execute(delete(plugin_data).where(plugin_data.c.plugin_id == plugin_id))

    async def list_plugin_data(self, plugin_id: str) -> dict[str, Any]:
        """List all data entries for a plugin."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(plugin_data.c.key, plugin_data.c.value).where(
                    plugin_data.c.plugin_id == plugin_id
                )
            )
            out = {}
            for row in result.fetchall():
                try:
                    out[row[0]] = json.loads(row[1])
                except (json.JSONDecodeError, TypeError):
                    out[row[0]] = row[1]
            return out
