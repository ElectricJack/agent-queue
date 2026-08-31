"""Append-only task comments and atomic, fenced description updates."""

from __future__ import annotations

import time
import uuid

from sqlalchemy import func, insert, select, update

from src.database.tables import archived_tasks, sessions, task_comments, tasks

MAX_COMMENT_BODY = 16000


class TaskFindingsConflict(Exception):
    """The task, description, or authorizing claim changed before the write."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _write_predicates(task_id: str, fence: dict | None):
    predicates = [tasks.c.id == task_id]
    if fence:
        predicates.append(tasks.c.project_id == fence["project_id"])
        if fence.get("session_id"):
            predicates.extend(
                [
                    tasks.c.claim_epoch == fence["claim_epoch"],
                    tasks.c.assigned_agent_id == fence["agent_id"],
                    select(sessions.c.id)
                    .where(
                        sessions.c.id == fence["session_id"],
                        sessions.c.task_id == task_id,
                        sessions.c.project_id == fence["project_id"],
                        sessions.c.agent_id == fence["agent_id"],
                        sessions.c.instance_token == fence["instance_token"],
                        sessions.c.state.in_(("starting", "running", "draining")),
                    )
                    .exists(),
                ]
            )
    return predicates


class TaskCommentQueriesMixin:
    async def _write_task_findings(
        self, conn, task_id, values, *, fence=None, expected_description=None
    ):
        predicates = _write_predicates(task_id, fence)
        stmt = update(tasks).where(*predicates)
        if expected_description is not None:
            stmt = stmt.where(tasks.c.description == expected_description)
        result = await conn.execute(stmt.values(updated_at=time.time(), **values))
        if result.rowcount != 1:
            current = (await conn.execute(select(tasks.c.id).where(*predicates))).first()
            code = (
                "description_conflict"
                if current and expected_description is not None
                else "stale_claim"
            )
            raise TaskFindingsConflict(code)

    async def update_task_description(
        self,
        task_id: str,
        description: str,
        *,
        expected_description: str | None = None,
        fence: dict | None = None,
    ) -> None:
        """Compare and set in SQL, before any legacy task_set side effects."""
        if not isinstance(description, str) or (
            expected_description is not None and not isinstance(expected_description, str)
        ):
            raise ValueError("description and expected_description must be strings")
        async with self._engine.begin() as conn:
            await self._write_task_findings(
                conn,
                task_id,
                {"description": description},
                fence=fence,
                expected_description=expected_description,
            )

    async def add_task_comment(
        self,
        task_id: str,
        body: str,
        *,
        author_kind: str,
        author_id: str,
        fence: dict | None = None,
    ) -> dict:
        """Append a server-authored comment; the caller derives identity from scope."""
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_COMMENT_BODY:
            raise ValueError(
                f"body must contain 1 to {MAX_COMMENT_BODY} characters and not be blank"
            )
        if author_kind not in {"user", "agent", "supervisor"} or not author_id:
            raise ValueError("invalid comment author")
        comment = {
            "id": "comment-" + uuid.uuid4().hex,
            "task_id": task_id,
            "body": body,
            "author_kind": author_kind,
            "author_id": author_id,
            "created_at": time.time(),
        }
        async with self._engine.begin() as conn:
            # This UPDATE locks the task, fences a reclaimed claim, and shares
            # the insert transaction so deletion cannot strand a comment.
            await self._write_task_findings(conn, task_id, {}, fence=fence)
            project_id = (await conn.execute(
                select(tasks.c.project_id).where(tasks.c.id == task_id)
            )).scalar_one()
            await conn.execute(insert(task_comments).values(**comment, project_id=project_id))
        return comment

    async def list_task_comments(
        self,
        task_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        project_id: str | None = None,
    ) -> dict:
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..200 and offset must be nonnegative")
        # Prefer the current task, falling back to its archive only when no
        # active task exists. Ownership is stored per comment, so legacy ID
        # reuse cannot merge two projects' history. Unknown ownership stays hidden.
        active = select(tasks.c.id).where(tasks.c.id == task_id)
        archived = select(archived_tasks.c.id).where(archived_tasks.c.id == task_id)
        identity = project_id if project_id is not None else func.coalesce(
            select(tasks.c.project_id).where(tasks.c.id == task_id).scalar_subquery(),
            select(archived_tasks.c.project_id)
            .where(archived_tasks.c.id == task_id).scalar_subquery(),
        )
        predicates = [
            task_comments.c.task_id == task_id,
            task_comments.c.project_id == identity,
            ~active.where(tasks.c.project_id != identity).exists(),
            active.where(tasks.c.project_id == identity).exists()
            | archived.where(archived_tasks.c.project_id == identity).exists(),
        ]
        async with self._engine.connect() as conn:
            total = (
                await conn.execute(
                    select(func.count())
                    .select_from(task_comments)
                    .where(
                        *predicates,
                    )
                )
            ).scalar_one()
            rows = (
                await conn.execute(
                    select(*(c for c in task_comments.c if c.name != "project_id"))
                    .where(
                        *predicates,
                    )
                    .order_by(task_comments.c.created_at.desc(), task_comments.c.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).mappings()
            return {
                "comments": [dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
