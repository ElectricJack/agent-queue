"""Task result operations."""

from __future__ import annotations

import json
import time
import uuid


class ResultQueryMixin:
    """Query mixin for task result operations.  Expects ``self._db``."""

    async def save_task_result(
        self, task_id: str, agent_id: str, output,
    ) -> None:
        """Persist an AgentOutput to the task_results table."""
        await self._db.execute(
            "INSERT INTO task_results (id, task_id, agent_id, result, summary, "
            "files_changed, error_message, tokens_used, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), task_id, agent_id, output.result.value,
             output.summary, json.dumps(output.files_changed),
             output.error_message, output.tokens_used, time.time()),
        )
        await self._db.commit()

    async def get_task_result(self, task_id: str) -> dict | None:
        """Return the most recent result for a task."""
        cursor = await self._db.execute(
            "SELECT * FROM task_results WHERE task_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_task_result(row)

    async def get_task_results(self, task_id: str) -> list[dict]:
        """Return all results for a task (retry history)."""
        cursor = await self._db.execute(
            "SELECT * FROM task_results WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_task_result(r) for r in rows]

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
