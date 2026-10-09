"""Task result operations."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict

from sqlalchemy import and_, delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.queries.task_queries import ACCEPTED_CLOSE_KEY
from src.database.tables import task_completion_records, task_metadata, task_results, tasks
from src.models import TaskCompletion

#: ``task_metadata`` key holding the completion record a close is about to
#: commit, bound to that close's identity (``completion_draft``).  ``task
#: close`` writes it before the terminal transition and deletes it once the
#: record is saved or the close is refused, so a restart in between leaves the
#: agent-supplied account (tests, commands, summary) recoverable.
PENDING_COMPLETION_KEY = "pending_completion"


def close_identity(completion_id: str, *, session_id: str | None, claim_epoch: int) -> dict:
    """The identity one ``task close`` attempt drafts and its transition records.

    The close passes it to its terminal transition as ``accepted_close``, which
    stores it as ``ACCEPTED_CLOSE_KEY`` in the status write's transaction.
    """
    return {
        "completion_id": completion_id,
        "session_id": session_id,
        "claim_epoch": int(claim_epoch),
    }


def completion_draft(completion: TaskCompletion, identity: dict) -> dict:
    """The ``PENDING_COMPLETION_KEY`` value: the record bound to its close."""
    return {"identity": identity, "completion": asdict(completion)}


def _draft_completion_id(draft) -> str | None:
    try:
        return draft["identity"]["completion_id"]
    except (KeyError, TypeError):
        return None


def _accepted_completion(
    task_id: str, draft_value: str, accepted_value: str | None, claim_epoch: int
) -> TaskCompletion | None:
    """The draft's record when its own transition provably committed, else None.

    Proof is positive and exact: the task's ``ACCEPTED_CLOSE_KEY`` names the
    draft's completion id, session and claim epoch, and the task is still on
    that claim epoch.  Status is never consulted -- READY, DEFINED, FAILED or
    CANCELLED reached any other way proves nothing about this close.
    """
    try:
        draft = json.loads(draft_value)
        accepted = json.loads(accepted_value) if accepted_value is not None else None
        identity = draft["identity"]
        completion = TaskCompletion(**draft["completion"])
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not isinstance(identity, dict)
        or identity != accepted
        or identity.get("completion_id") != completion.id
        or identity.get("claim_epoch") != claim_epoch
        or completion.task_id != task_id
    ):
        return None
    return completion


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

    async def save_task_completion(
        self, completion: TaskCompletion, *, idempotent: bool = False
    ) -> None:
        """Append a close record; a fenced replay may reuse its durable identity."""
        async with self._engine.begin() as conn:
            statement = pg_insert(task_completion_records).values(
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
            if idempotent:
                statement = statement.on_conflict_do_nothing(index_elements=["id"])
            await conn.execute(statement)
            if completion.outcome == "fail" or completion.work_outcome == "abandoned":
                from src.integration.branch_retirement import request_task_retirement_on

                await request_task_retirement_on(
                    conn, completion.task_id, request_id="close:" + completion.id,
                    reason="failed close" if completion.outcome == "fail" else "abandoned close",
                )
            flipped = await self.recompute_blocked({completion.task_id}, conn=conn)
        await self.log_blocked_flips(flipped)

    async def recover_pending_completions(self) -> list[str]:
        """Save the drafted completion of every accepted close a restart interrupted.

        Runs once at daemon start, before stale-state recovery moves any task.
        A draft is recovered only when its close's own terminal transition
        recorded the draft's identity (``ACCEPTED_CLOSE_KEY``) and the task is
        still on that claim epoch: then only ``save_task_completion`` was
        lost, and the draft is saved under its own id (idempotent, so a replay
        never duplicates it).  Every other draft -- a close that never
        committed, was refused, or was overtaken by a later claim, whatever
        status the task reached since -- is dropped.  Nothing is invented: the
        record carries exactly what the agent submitted.
        """
        accepted = task_metadata.alias("accepted_close")
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(
                        task_metadata.c.task_id,
                        task_metadata.c.value.label("draft"),
                        accepted.c.value.label("accepted"),
                        tasks.c.claim_epoch,
                    )
                    .join(tasks, tasks.c.id == task_metadata.c.task_id)
                    .outerjoin(
                        accepted,
                        and_(
                            accepted.c.task_id == task_metadata.c.task_id,
                            accepted.c.key == ACCEPTED_CLOSE_KEY,
                        ),
                    )
                    .where(task_metadata.c.key == PENDING_COMPLETION_KEY)
                )
            ).mappings().all()
        recovered: list[str] = []
        for row in rows:
            task_id = row["task_id"]
            completion = _accepted_completion(
                task_id, row["draft"], row["accepted"], row["claim_epoch"]
            )
            if completion is not None:
                await self.save_task_completion(completion, idempotent=True)
                recovered.append(task_id)
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(task_metadata).where(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == PENDING_COMPLETION_KEY,
                        task_metadata.c.value == row["draft"],
                    )
                )
        return recovered

    async def discard_pending_completion(
        self, task_id: str, completion_id: str, *, keep_accepted: bool = True
    ) -> bool:
        """Delete the draft of close *completion_id*; True when it was removed.

        A refused or failed close calls this with ``keep_accepted=True``: a
        draft whose transition did commit (``ACCEPTED_CLOSE_KEY`` names it)
        stays for ``recover_pending_completions``, because only its record
        was lost.  After the record is saved, ``keep_accepted=False`` clears
        it.  A draft belonging to another close attempt is never touched.
        """
        async with self._engine.begin() as conn:
            rows = {
                row.key: row.value
                for row in (
                    await conn.execute(
                        select(task_metadata.c.key, task_metadata.c.value)
                        .where(
                            task_metadata.c.task_id == task_id,
                            task_metadata.c.key.in_(
                                (PENDING_COMPLETION_KEY, ACCEPTED_CLOSE_KEY)
                            ),
                        )
                        .with_for_update()
                    )
                ).all()
            }
            draft_value = rows.get(PENDING_COMPLETION_KEY)
            if draft_value is None:
                return False
            try:
                draft_id = _draft_completion_id(json.loads(draft_value))
                accepted = json.loads(rows.get(ACCEPTED_CLOSE_KEY) or "null")
            except ValueError:
                return False
            if draft_id != completion_id:
                return False
            if (
                keep_accepted
                and isinstance(accepted, dict)
                and accepted.get("completion_id") == completion_id
            ):
                return False
            await conn.execute(
                delete(task_metadata).where(
                    task_metadata.c.task_id == task_id,
                    task_metadata.c.key == PENDING_COMPLETION_KEY,
                    task_metadata.c.value == draft_value,
                )
            )
        return True

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
