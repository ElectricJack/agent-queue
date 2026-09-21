"""Document-review command surface and command-layer authorization."""

from __future__ import annotations

import json
import logging
from typing import Any

from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal
from src.models import TaskStatus
from src.reviews.service import ReviewError, ReviewHooks, ReviewService

logger = logging.getLogger(__name__)

_LIVE = frozenset({"starting", "running", "draining"})


def _error(code: str, message: str) -> dict[str, Any]:
    """Return the stable review-command refusal envelope."""
    return {"success": False, "error_code": code, "error": message}


class ReviewCommandsMixin:
    """``review_*`` handlers, while :mod:`src.reviews.service` owns state."""

    def _review_service(self) -> ReviewService:
        return ReviewService(
            self.db,
            self.config.vault_root,
            ReviewHooks(
                emit=self._emit_review_event,
                resolve_gate=lambda gate_id, by, resolution: self.orchestrator._resolve_gate_and_emit(
                    gate_id, resolved_by=by, resolution=resolution
                ),
                changes_requested=self._on_review_changes_requested,
            ),
        )

    async def _emit_review_event(self, event_type: str, payload: dict) -> None:
        """Persist then fan out a review event; neither observer may undo a write."""
        body = dict(payload)
        try:
            body["seq"] = await self.db.log_event(
                event_type,
                project_id=payload.get("project_id"),
                task_id=payload.get("author_task_id") or payload.get("task_id"),
                payload=json.dumps(payload, default=str)[:4000],
            )
        except Exception:
            logger.warning("persisting %s failed", event_type, exc_info=True)
        try:
            await self.orchestrator.bus.emit(event_type, body)
        except Exception:
            logger.warning("emitting %s failed", event_type, exc_info=True)

    async def _review_decider_label(self, review: dict) -> tuple[str | None, dict | None]:
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.LOCAL:
            return "human:local-operator", None
        if (
            principal.kind is PrincipalKind.SESSION
            and principal.elevated
            and review["decider"] == "user_or_supervisor"
        ):
            row = await self.db.get_session(principal.session_id)
            if (
                row is not None
                and row.profile_id == "supervisor"
                and row.lifecycle == "named"
                and row.state in _LIVE
                and row.desired_state == "running"
                and row.project_id in (None, review["project_id"])
            ):
                return f"supervisor (delegated) session:{principal.session_id}", None
        why = (
            "this review is not delegated to the supervisor"
            if review["decider"] == "user"
            else "a live supervisor session is required"
        )
        return None, _error("not_decider", f"only Jack (dashboard or local CLI) may decide: {why}")

    async def _review_for_caller(self, review_id: object) -> tuple[dict | None, dict | None]:
        """Load a review and apply the project boundary before revealing it."""
        if not isinstance(review_id, str) or not review_id:
            return None, _error("not_found", "review_id is required")
        review = await self.db.get_review(review_id)
        if review is None:
            return None, _error("not_found", f"review {review_id!r} was not found")
        principal = current_principal() or TRUSTED_LOCAL
        if (
            principal.kind is PrincipalKind.SESSION
            and principal.project_id is not None
            and principal.project_id != review["project_id"]
        ):
            return None, _error("not_your_task", "review belongs to another project")
        return review, None

    async def _worker_held_task(self) -> tuple[object | None, dict | None]:
        """Return the current worker's held task, or the review-specific refusal."""
        held_id = await self._scoped_held_task_id()
        task = await self.db.get_task(held_id) if held_id else None
        if task is None:
            return None, _error("not_your_task", "a worker may submit only from its held task")
        return task, None

    @staticmethod
    def _is_worker() -> bool:
        principal = current_principal() or TRUSTED_LOCAL
        return principal.kind is PrincipalKind.SESSION and not principal.elevated

    async def _submit_project(self, args: dict) -> tuple[str | None, str | None, dict | None]:
        """Resolve a new review's project and optional author task."""
        principal = current_principal() or TRUSTED_LOCAL
        task_id = args.get("task_id")
        if self._is_worker():
            held, error = await self._worker_held_task()
            if error:
                return None, None, error
            if task_id != held.id:
                return None, None, _error("not_your_task", "task_id must be the task this worker holds")
            if args.get("project_id") not in (None, held.project_id):
                return None, None, _error("not_your_task", "project belongs to another task")
            return held.project_id, held.id, None
        if task_id is not None:
            task = await self.db.get_task(str(task_id))
            if task is None:
                return None, None, _error("not_found", f"task {task_id!r} was not found")
            if (
                principal.kind is PrincipalKind.SESSION
                and principal.project_id is not None
                and task.project_id != principal.project_id
            ):
                return None, None, _error("not_your_task", "task belongs to another project")
            return task.project_id, task.id, None
        project_id = args.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            return None, None, _error("not_found", "project_id is required when task_id is omitted")
        project = await self.db.get_project(project_id)
        if project is None:
            return None, None, _error("not_found", f"project {project_id!r} was not found")
        if (
            principal.kind is PrincipalKind.SESSION
            and principal.project_id is not None
            and project_id != principal.project_id
        ):
            return None, None, _error("not_your_task", "project belongs to another session")
        return project_id, None, None

    async def _cmd_review_submit(self, args: dict) -> dict:
        service = self._review_service()
        principal = current_principal() or TRUSTED_LOCAL
        content = args.get("content")
        if not isinstance(content, str):
            return _error("not_utf8", "content must be UTF-8 text")
        review_id = args.get("review_id")
        try:
            if review_id:
                review, error = await self._review_for_caller(review_id)
                if error:
                    return error
                submitted_task_id: str | None = None
                if self._is_worker():
                    held, error = await self._worker_held_task()
                    if error:
                        return error
                    if review["author_task_id"] != held.id:
                        return _error("not_your_task", "only the author task may revise this review")
                    submitted_task_id = held.id
                return {
                    "success": True,
                    **await service.revise(
                        review_id=review["id"],
                        content=content,
                        changes_note=str(args.get("changes") or ""),
                        resolves=list(args.get("resolves") or []),
                        submitted_by=principal.describe(),
                        submitted_task_id=submitted_task_id,
                    ),
                }
            project_id, author_task_id, error = await self._submit_project(args)
            if error:
                return error
            project = await self.db.get_project(project_id)
            return {
                "success": True,
                **await service.submit(
                    project_id=project_id,
                    author_task_id=author_task_id,
                    kind=str(args.get("kind") or ""),
                    title=str(args.get("title") or ""),
                    content=content,
                    submitted_by=principal.describe(),
                    decider=(
                        "user_or_supervisor"
                        if project is not None and project.review_delegate_to == "supervisor"
                        else "user"
                    ),
                ),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _cmd_review_show(self, args: dict) -> dict:
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        try:
            return {
                "success": True,
                **await self._review_service().show(
                    review_id=review["id"],
                    revision=args.get("revision"),
                    comments=bool(args.get("comments", False)),
                    diff_from=args.get("diff_from"),
                ),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _cmd_review_list(self, args: dict) -> dict:
        principal = current_principal() or TRUSTED_LOCAL
        project_id = args.get("project_id")
        task_id = args.get("task_id")
        if principal.kind is PrincipalKind.SESSION and principal.project_id is not None:
            if project_id not in (None, principal.project_id):
                return _error("not_your_task", "project belongs to another session")
            project_id = principal.project_id
        if self._is_worker():
            held, error = await self._worker_held_task()
            if error:
                return error
            if task_id not in (None, held.id):
                return _error("not_your_task", "task_id must be the task this worker holds")
            task_id = held.id
        return {
            "success": True,
            "reviews": await self.db.list_reviews(
                project_id=project_id,
                state=args.get("state"),
                kind=args.get("kind"),
                task_id=task_id,
            ),
        }

    async def _cmd_review_withdraw(self, args: dict) -> dict:
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        if self._is_worker():
            held, error = await self._worker_held_task()
            if error:
                return error
            if review["author_task_id"] != held.id:
                return _error("not_decider", "only the author task may withdraw this review")
        try:
            return {
                "success": True,
                **await self._review_service().withdraw(
                    review_id=review["id"],
                    reason=str(args.get("reason") or ""),
                    by=(current_principal() or TRUSTED_LOCAL).describe(),
                ),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _cmd_review_decide(self, args: dict) -> dict:
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        label, error = await self._review_decider_label(review)
        if error:
            return error
        decision = args.get("decision")
        if decision not in {"approve", "request_changes"}:
            return _error("not_in_review", "decision must be approve or request_changes")
        try:
            return {
                "success": True,
                **await self._review_service().decide(
                    review_id=review["id"],
                    revision=args.get("revision"),
                    approve=decision == "approve",
                    note=str(args.get("note") or ""),
                    decided_by=label,
                ),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _cmd_review_comment(self, args: dict) -> dict:
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        label, error = await self._review_decider_label(review)
        if error:
            return error
        try:
            return {
                "success": True,
                **await self._review_service().comment(
                    review_id=review["id"],
                    revision=args.get("revision"),
                    quote=args.get("quote"),
                    heading_path=list(args.get("heading_path") or []),
                    body=str(args.get("body") or ""),
                    author=label,
                ),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _cmd_review_delegate(self, args: dict) -> dict:
        if (current_principal() or TRUSTED_LOCAL).kind is not PrincipalKind.LOCAL:
            return _error("local_operator_only", "review delegation requires local operator")
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        try:
            return {
                "success": True,
                **await self._review_service().delegate(
                    review_id=review["id"], to=str(args.get("to") or "")
                ),
            }
        except (ReviewError, ValueError) as error:
            return _error(getattr(error, "code", "not_found"), str(error))

    async def _cmd_review_import_edits(self, args: dict) -> dict:
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.LOCAL:
            return _error("local_operator_only", "importing review edits requires local operator")
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        try:
            return {
                "success": True,
                **await self._review_service().import_edits(review_id=review["id"], by=principal.describe()),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _on_review_changes_requested(self, review: dict, feedback: str) -> None:
        """Return requested changes to the author without keeping a worker held."""
        task_id = review.get("author_task_id")
        if not task_id:
            await self._cmd_message_send(
                {
                    "project_id": review["project_id"],
                    "to_kind": "session",
                    "to_id": f"supervisor-{review['project_id']}",
                    "from_kind": "system",
                    "from_id": f"review:{review['id']}",
                    "body": feedback,
                    "subject": f"Review {review['id']} needs changes",
                }
            )
            return

        task = await self.db.get_task(task_id)
        if task is not None:
            if task.status is TaskStatus.IN_PROGRESS:
                await self._cmd_task_comment({"task_id": task.id, "body": feedback})
                return
            updated = task.description + "\n\n---\n**Review feedback:**\n" + feedback
            await self.db.transition_task(
                task.id,
                TaskStatus.READY,
                context="review_changes_requested",
                description=updated,
                retry_count=0,
                assigned_agent_id=None,
                pr_url=None,
            )
            await self.db.add_task_context(
                task.id, type="reopen_feedback", label="Review feedback", content=feedback
            )
            await self.db.log_event(
                "review_changes_requested",
                project_id=task.project_id,
                task_id=task.id,
                payload=review["id"],
            )
            return

        archived = await self.db.get_archived_task(task_id)
        title = review["title"]
        project_id = review["project_id"]
        if archived is not None:
            title = archived["title"]
            project_id = archived["project_id"]
        parent_id = archived.get("parent_task_id") if archived else None
        if parent_id is not None and await self.db.get_task(parent_id) is None:
            parent_id = None
        await self._cmd_create_task(
            {
                "project_id": project_id,
                "title": f"Revise {title} (review {review['id']})",
                "description": feedback,
                "profile_id": archived.get("profile_id") if archived else None,
                "intelligence_class": archived.get("intelligence_class") if archived else None,
                "parent_id": parent_id,
                "root": parent_id is None,
            }
        )
