"""Task result operations."""

from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import insert, select

from src.database.tables import task_results


class ResultQueryMixin:
    """Query mixin for task result operations.  Expects ``self._engine``."""

    async def save_task_result(
        self,
        task_id: str,
        agent_id: str,
        output,
    ) -> None:
        """Persist an AgentOutput to the task_results table."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(task_results).values(
                    id=str(uuid.uuid4()),
                    task_id=task_id,
                    agent_id=agent_id,
                    result=output.result.value,
                    summary=output.summary,
                    files_changed=json.dumps(output.files_changed),
                    error_message=output.error_message,
                    tokens_used=output.tokens_used,
                    created_at=time.time(),
                )
            )

    async def get_task_result(self, task_id: str) -> dict | None:
        """Return the most recent result for a task."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_results)
                .where(task_results.c.task_id == task_id)
                .order_by(task_results.c.created_at.desc())
                .limit(1)
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_task_result(row)

    async def get_task_results(self, task_id: str) -> list[dict]:
        """Return all results for a task (retry history)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_results)
                .where(task_results.c.task_id == task_id)
                .order_by(task_results.c.created_at.asc())
            )
            return [self._row_to_task_result(r) for r in result.mappings().fetchall()]

    @staticmethod
    def _row_to_task_result(row) -> dict:
        """Convert a database row to a task result dict."""
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "agent_id": row["agent_id"],
            "result": row["result"],
            "summary": row["summary"],
            "files_changed": json.loads(row["files_changed"]),
            "error_message": row["error_message"],
            "tokens_used": row["tokens_used"],
            "created_at": row["created_at"],
        }
