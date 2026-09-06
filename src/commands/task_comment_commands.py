"""Task findings permissions and authored comments (append-only for agents)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from src.database.queries.task_comment_queries import MAX_COMMENT_BODY, TaskFindingsConflict

logger = logging.getLogger(__name__)


class TaskCommentCommandsMixin:
    def _task_findings_scope_error(self, task) -> dict | None:
        scope = self._current_scope or {}
        if scope.get("kind") != "session":
            return None
        if not scope.get("session_id"):
            return {"error": "out of scope: session identity is required"}
        project_id = scope.get("project_id")
        if project_id is not None and task.project_id != project_id:
            return {"error": "out of scope: task belongs to another project"}
        if not scope.get("elevated"):
            if project_id is None:
                return {"error": "out of scope: no assigned project"}
            if scope.get("task_id") not in (None, task.id):
                return {"error": "out of scope: task_id mismatch"}
        return None

    async def _reviewer_grant(self, task) -> str | None:
        """Author id when the caller is the live reviewer *of* ``task``, else None.

        A reviewer reads a task it deliberately does not own, so
        ``_task_findings_scope_error``'s ``task_id`` pin refuses it by
        construction. Authority comes from the same verified review assignment
        the API scope check uses, so "is a reviewer of this task" has exactly
        one definition in the codebase.
        """
        from src.api.auth import RequestScope
        from src.api.scope import reviewed_task_for_reviewer

        scope = self._current_scope or {}
        if scope.get("kind") != "session" or scope.get("elevated"):
            return None
        session_id = scope.get("session_id")
        if not session_id:
            return None
        request_scope = RequestScope(
            kind="session",
            session_id=session_id,
            task_id=scope.get("task_id"),
            project_id=scope.get("project_id"),
        )
        if await reviewed_task_for_reviewer(self.db, request_scope) != task.id:
            return None
        session = await self.db.get_session(session_id)
        return (session.agent_id if session is not None else None) or session_id

    async def _task_findings_write_fence(self, task, args) -> tuple[dict | None, dict | None]:
        error = self._task_findings_scope_error(task)
        if error:
            return None, error
        scope = self._current_scope or {}
        fence = {"project_id": task.project_id}
        if scope.get("kind") != "session" or scope.get("elevated"):
            return fence, None
        epoch = args.get("claim_epoch")
        if epoch is not None and (type(epoch) is not int or epoch < 0):
            return None, {"error": "claim_epoch must be a nonnegative integer"}
        error = await self._assert_session_owns(
            task.id, session_id=scope["session_id"], claim_epoch=epoch
        )
        if error:
            return None, error
        session = await self.db.get_session(scope["session_id"])
        if (
            session is None
            or session.project_id != task.project_id
            or session.task_id != task.id
            or session.state not in {"starting", "running", "draining"}
            or session.agent_id != task.assigned_agent_id
        ):
            return None, {"error": "out of scope: this session no longer owns the task"}
        fence.update(
            session_id=session.id,
            instance_token=session.instance_token,
            agent_id=session.agent_id,
            claim_epoch=task.claim_epoch,
        )
        return fence, None

    @staticmethod
    def _task_findings_conflict(error: TaskFindingsConflict) -> dict:
        if error.code == "description_conflict":
            return {
                "error": "Description changed; reload the task and preserve the newer edits before retrying.",
                "error_code": error.code,
            }
        return {
            "error": "Task or claim changed; reload the task before retrying.",
            "error_code": error.code,
            "result": error.code,
        }

    async def _emit_task_findings_updated(self, task) -> None:
        # Comments are not messages and cannot resolve gates. Publish only
        # identifiers to the existing invalidation event, never their text.
        payload = {"task_id": task.id, "project_id": task.project_id}
        try:
            payload["seq"] = await self.db.log_event(
                "task.updated", project_id=task.project_id, task_id=task.id
            )
        except Exception:
            logger.warning("Could not persist task.updated for %s", task.id, exc_info=True)
        try:
            await self.orchestrator.bus.emit("task.updated", payload)
        except Exception:
            logger.warning("Could not publish task.updated for %s", task.id, exc_info=True)

    async def _cmd_task_comment(self, args: dict) -> dict:
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}
        body = args.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_COMMENT_BODY:
            return {
                "error": f"body must contain 1 to {MAX_COMMENT_BODY} characters and not be blank"
            }
        if any(key in args for key in ("author_kind", "author_id", "created_at", "id")):
            return {"error": "Comment author, id, and timestamp are assigned by the server"}
        task = await self.db.get_task(task_id)
        if task is None:
            return {"error": f"Task '{task_id}' not found"}
        fence, error = await self._task_findings_write_fence(task, args)
        if error:
            return error
        scope = self._current_scope or {}
        if scope.get("kind") != "session":
            author_kind, author_id = "user", "local"
        elif scope.get("elevated"):
            author_kind, author_id = "supervisor", scope["session_id"]
        else:
            author_kind, author_id = "agent", fence["agent_id"] or scope["session_id"]
        try:
            comment = await self.db.add_task_comment(
                task_id,
                body,
                author_kind=author_kind,
                author_id=author_id,
                fence=fence,
            )
        except TaskFindingsConflict as error:
            return self._task_findings_conflict(error)
        await self._emit_task_findings_updated(task)
        return {"comment": comment}

    async def _comment_mutation_target(self, args: dict) -> tuple[object | None, dict | None]:
        """Resolve the task behind an edit/delete, refusing every agent caller.

        Comments are append-only for agents (a worker's findings are part of
        the task's audit trail); only the operator surfaces — the dashboard
        and the local CLI, which run without a session scope — may rewrite
        history.
        """
        task_id, comment_id = args.get("task_id"), args.get("comment_id")
        if not task_id or not comment_id:
            return None, {"error": "task_id and comment_id are required"}
        scope = self._current_scope or {}
        if scope.get("kind") == "session":
            return None, {"error": "out of scope: comments are append-only for agent sessions"}
        task = await self.db.get_task(task_id)
        if task is None:
            return None, {"error": f"Task '{task_id}' not found"}
        return task, None

    async def _cmd_task_comment_edit(self, args: dict) -> dict:
        body = args.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_COMMENT_BODY:
            return {
                "error": f"body must contain 1 to {MAX_COMMENT_BODY} characters and not be blank"
            }
        task, error = await self._comment_mutation_target(args)
        if error:
            return error
        try:
            comment = await self.db.update_task_comment(
                args["comment_id"], body, task_id=task.id, project_id=task.project_id
            )
        except TaskFindingsConflict as error:
            return self._task_findings_conflict(error)
        if comment is None:
            return {"error": f"Comment '{args['comment_id']}' not found on task '{task.id}'"}
        await self._emit_task_findings_updated(task)
        return {"comment": comment}

    async def _cmd_task_comment_delete(self, args: dict) -> dict:
        task, error = await self._comment_mutation_target(args)
        if error:
            return error
        try:
            comment = await self.db.delete_task_comment(
                args["comment_id"], task_id=task.id, project_id=task.project_id
            )
        except TaskFindingsConflict as error:
            return self._task_findings_conflict(error)
        if comment is None:
            return {"error": f"Comment '{args['comment_id']}' not found on task '{task.id}'"}
        await self._emit_task_findings_updated(task)
        return {"deleted": comment["id"], "task_id": task.id}

    async def _cmd_task_comments(self, args: dict) -> dict:
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}
        limit, offset = args.get("limit", 50), args.get("offset", 0)
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            return {"error": "limit must be 1..200 and offset must be nonnegative"}
        task = await self.db.get_task(task_id)
        if task is None:
            archived = await self.db.get_archived_task(task_id)
            if archived is None:
                return {"error": f"Task '{task_id}' not found"}
            task = SimpleNamespace(id=task_id, project_id=archived["project_id"])
        error = self._task_findings_scope_error(task)
        if error and await self._reviewer_grant(task) is None:
            return error
        return await self.db.list_task_comments(
            task_id, limit=limit, offset=offset, project_id=task.project_id
        )
