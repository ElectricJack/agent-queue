"""Plugin and plugin_data CRUD operations."""

from __future__ import annotations

import json
import time
from typing import Any


class PluginQueryMixin:
    """Query mixin for plugin operations.  Expects ``self._db``."""

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
        await self._db.execute(
            "INSERT OR IGNORE INTO plugins "
            "(id, version, source_url, source_rev, source_branch, "
            "install_path, status, config, permissions, error_message, "
            "installed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                plugin_id,
                version,
                source_url,
                source_rev,
                source_branch,
                install_path,
                status,
                config,
                permissions,
                now,
                now,
            ),
        )
        await self._db.commit()

    async def get_plugin(self, plugin_id: str) -> dict | None:
        """Fetch a single plugin by ID."""
        cursor = await self._db.execute("SELECT * FROM plugins WHERE id = ?", (plugin_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_plugin_dict(row)

    async def list_plugins(
        self,
        status: str | None = None,
    ) -> list[dict]:
        """List plugins with optional status filter."""
        if status:
            cursor = await self._db.execute("SELECT * FROM plugins WHERE status = ?", (status,))
        else:
            cursor = await self._db.execute("SELECT * FROM plugins")
        rows = await cursor.fetchall()
        return [self._row_to_plugin_dict(r) for r in rows]

    async def update_plugin(self, plugin_id: str, **kwargs) -> None:
        """Update arbitrary plugin fields."""
        if not kwargs:
            return
        sets = []
        vals = []
        for key, value in kwargs.items():
            sets.append(f"{key} = ?")
            vals.append(value)
        sets.append("updated_at = ?")
        vals.append(time.time())
        vals.append(plugin_id)
        await self._db.execute(f"UPDATE plugins SET {', '.join(sets)} WHERE id = ?", vals)
        await self._db.commit()

    async def delete_plugin(self, plugin_id: str) -> None:
        """Delete a plugin record."""
        await self._db.execute("DELETE FROM plugin_data WHERE plugin_id = ?", (plugin_id,))
        await self._db.execute("DELETE FROM plugins WHERE id = ?", (plugin_id,))
        await self._db.commit()

    # --- Plugin Data ---

    async def get_plugin_data(self, plugin_id: str, key: str) -> Any:
        """Fetch a single plugin data value."""
        cursor = await self._db.execute(
            "SELECT value FROM plugin_data WHERE plugin_id = ? AND key = ?",
            (plugin_id, key),
        )
        row = await cursor.fetchone()
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
        await self._db.execute(
            "INSERT INTO plugin_data (plugin_id, key, value, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(plugin_id, key) DO UPDATE SET value = ?, updated_at = ?",
            (plugin_id, key, json_value, now, json_value, now),
        )
        await self._db.commit()

    async def delete_plugin_data(self, plugin_id: str, key: str) -> None:
        """Delete a single plugin data entry."""
        await self._db.execute(
            "DELETE FROM plugin_data WHERE plugin_id = ? AND key = ?",
            (plugin_id, key),
        )
        await self._db.commit()

    async def delete_plugin_data_all(self, plugin_id: str) -> None:
        """Delete all data for a plugin."""
        await self._db.execute("DELETE FROM plugin_data WHERE plugin_id = ?", (plugin_id,))
        await self._db.commit()

    async def list_plugin_data(self, plugin_id: str) -> dict[str, Any]:
        """List all data entries for a plugin."""
        cursor = await self._db.execute(
            "SELECT key, value FROM plugin_data WHERE plugin_id = ?",
            (plugin_id,),
        )
        rows = await cursor.fetchall()
        result = {}
        for row in rows:
            try:
                result[row[0]] = json.loads(row[1])
            except (json.JSONDecodeError, TypeError):
                result[row[0]] = row[1]
        return result

    # --- Helper ---

    @staticmethod
    def _row_to_plugin_dict(row) -> dict:
        """Convert a database row to a plugin dict."""
        if hasattr(row, "keys"):
            # Row is a sqlite3.Row (dict-like)
            return dict(row)
        # Positional tuple — match column order from CREATE TABLE
        return {
            "id": row[0],
            "version": row[1],
            "source_url": row[2],
            "source_rev": row[3],
            "source_branch": row[4],
            "install_path": row[5],
            "status": row[6],
            "config": row[7],
            "permissions": row[8],
            "error_message": row[9],
            "installed_at": row[10],
            "updated_at": row[11],
        }
