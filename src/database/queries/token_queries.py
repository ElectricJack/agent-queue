"""Token ledger operations."""

from __future__ import annotations

import time
import uuid


class TokenQueryMixin:
    """Query mixin for token ledger operations.  Expects ``self._db``."""

    async def record_token_usage(
        self, project_id: str, agent_id: str, task_id: str, tokens: int,
    ) -> None:
        """Append a token usage record."""
        await self._db.execute(
            "INSERT INTO token_ledger (id, project_id, agent_id, task_id, "
            "tokens_used, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), project_id, agent_id, task_id, tokens, time.time()),
        )
        await self._db.commit()

    async def get_project_token_usage(
        self, project_id: str, since: float | None = None,
    ) -> int:
        """Return total tokens consumed by a project, optionally since a timestamp."""
        if since:
            cursor = await self._db.execute(
                "SELECT COALESCE(SUM(tokens_used), 0) as total "
                "FROM token_ledger WHERE project_id = ? AND timestamp >= ?",
                (project_id, since),
            )
        else:
            cursor = await self._db.execute(
                "SELECT COALESCE(SUM(tokens_used), 0) as total "
                "FROM token_ledger WHERE project_id = ?",
                (project_id,),
            )
        row = await cursor.fetchone()
        return row["total"]
