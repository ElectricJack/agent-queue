"""Task result operations."""

from __future__ import annotations

import json
import time
import uuid

from sqlalchemy import insert, select

from src.database.tables import task_completion_records, task_results
from src.models import TaskCompletion


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

    async def save_task_completion(self, completion: TaskCompletion) -> None:
        """Append one durable task-close record."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(task_completion_records).values(
                    id=completion.id,
                    task_id=completion.task_id,
                    outcome=completion.outcome,
                    work_outcome=completion.work_outcome,
                    failure_class=completion.failure_class,
                    changes=completion.changes,
                    verification=completion.verification,
                    tests=json.dumps(completion.tests),
                    commands=json.dumps(completion.commands),
                    branch=completion.branch,
                    commits=json.dumps(completion.commits),
                    pr_url=completion.pr_url,
                    summary=completion.summary,
                    notes=completion.notes,
                    deliverables=json.dumps(completion.deliverables),
                    completed_at=completion.completed_at,
                )
            )

    async def get_task_completion(self, task_id: str) -> TaskCompletion | None:
        """Return the latest completion record for *task_id*."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_completion_records)
                .where(task_completion_records.c.task_id == task_id)
                .order_by(task_completion_records.c.completed_at.desc())
                .limit(1)
            )
            row = result.mappings().fetchone()
            return self._row_to_task_completion(row) if row else None

    async def get_task_completions(self, task_id: str) -> list[TaskCompletion]:
        """Return every completion record for *task_id*, oldest first."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_completion_records)
                .where(task_completion_records.c.task_id == task_id)
                .order_by(task_completion_records.c.completed_at.asc())
            )
            return [self._row_to_task_completion(row) for row in result.mappings().fetchall()]

    @staticmethod
    def _row_to_task_completion(row) -> TaskCompletion:
        return TaskCompletion(
            id=row["id"],
            task_id=row["task_id"],
            outcome=row["outcome"],
            work_outcome=row["work_outcome"],
            failure_class=row["failure_class"],
            changes=row["changes"],
            verification=row["verification"],
            tests=json.loads(row["tests"]),
            commands=json.loads(row["commands"]),
            branch=row["branch"],
            commits=json.loads(row["commits"]),
            pr_url=row["pr_url"],
            summary=row["summary"],
            notes=row["notes"],
            deliverables=json.loads(row.get("deliverables") or "[]"),
            completed_at=row["completed_at"],
        )
