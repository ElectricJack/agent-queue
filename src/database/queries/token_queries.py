"""Token ledger operations."""

from __future__ import annotations

import time
import uuid

from sqlalchemy import func, insert, select

from src.database.tables import token_ledger


class TokenQueryMixin:
    """Query mixin for token ledger operations.  Expects ``self._engine``."""

    async def record_token_usage(
        self,
        project_id: str,
        agent_id: str,
        task_id: str,
        tokens: int,
    ) -> None:
        """Append a token usage record."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(token_ledger).values(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    tokens_used=tokens,
                    timestamp=time.time(),
                )
            )

    async def get_project_token_usage(
        self,
        project_id: str,
        since: float | None = None,
    ) -> int:
        """Return total tokens consumed by a project, optionally since a timestamp."""
        stmt = select(func.coalesce(func.sum(token_ledger.c.tokens_used), 0).label("total")).where(
            token_ledger.c.project_id == project_id
        )
        if since:
            stmt = stmt.where(token_ledger.c.timestamp >= since)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            row = result.fetchone()
            return row[0]
