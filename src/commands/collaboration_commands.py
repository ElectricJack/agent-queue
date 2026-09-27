"""Bounded collaboration threads between held tasks (collaboration spec §4).

Caller identity is always derived on the server. A worker session acts only
for the task it holds, under that task's live claim epoch; it never names a
member, sender or session. Only the operator and elevated sessions create
threads, remove members or close every active thread in a project. No command
here writes session activity or pane input.
"""

from __future__ import annotations

import logging
import time

from pydantic import ValidationError

from src.collaboration import CollaborationError
from src.commands.contracts.collaboration import (
    CollaborationAcceptArgs,
    CollaborationCloseArgs,
    CollaborationCreateArgs,
    CollaborationGetArgs,
    CollaborationListArgs,
    CollaborationMemberRecord,
    CollaborationMessageRecord,
    CollaborationThreadRecord,
)
from src.commands.message_commands import MESSAGES_DISABLED_ERROR
from src.commands.principal import PrincipalKind, current_principal
from src.models import TaskStatus

logger = logging.getLogger(__name__)

#: One answer for unknown, other-project and non-member threads, so a guessed
#: id learns nothing about threads the caller may not read.
_NOT_FOUND = "Collaboration thread not found"


def _refusal(exc: CollaborationError) -> dict:
    result = {"success": False, "error_code": exc.code, "error": str(exc)}
    if exc.retry_after is not None:
        result["retry_after"] = exc.retry_after
    return result


def _invalid(exc: Exception) -> dict:
    return {"success": False, "error_code": "collaboration.invalid", "error": str(exc)}


def _fields(model, row: dict) -> dict:
    return {key: row[key] for key in model.model_fields if key in row}


def _thread_view(thread: dict, now: float) -> dict:
    """Project a query row onto :class:`CollaborationThreadRecord`."""
    members = []
    for member in thread["members"]:
        needs_accept = member["state"] != "removed" and (
            member["state"] != "accepted"
            or member["accepted_claim_epoch"] != member["task_claim_epoch"]
        )
        members.append(
            _fields(CollaborationMemberRecord, {**member, "needs_accept": needs_accept})
        )
    remaining = max(0.0, thread["deadline_at"] - now) if thread["state"] == "active" else 0.0
    return _fields(
        CollaborationThreadRecord, {**thread, "remaining_seconds": remaining, "members": members}
    )


class CollaborationCommandsMixin:
    async def _collaboration_caller(self, args, *, mutation: bool) -> dict:
        """Derive who is calling, in the shape of ``_wait_identity``.

        ``kind`` is ``operator`` (loopback CLI), ``supervisor`` (an elevated
        session) or ``member`` (a worker session, resolved to its held task).
        A pool member's mutation must carry the live ``claim_epoch``.
        """
        principal = current_principal()
        if principal is None or principal.kind == PrincipalKind.LOCAL:
            return {
                "kind": "operator",
                "project_id": args.get("project_id"),
                "session_id": None,
                "instance_token": None,
                "task_id": None,
                "claim_epoch": None,
                "created_by_id": "operator",
            }
        if principal.kind != PrincipalKind.SESSION or not principal.session_id:
            raise CollaborationError("out_of_scope", "collaboration requires a session caller")
        session = await self.db.get_session(principal.session_id)
        if (
            not session
            or not principal.session_instance_token
            or session.instance_token != principal.session_instance_token
        ):
            raise CollaborationError("out_of_scope", "session instance does not match")
        if args.get("session_id", session.id) != session.id:
            raise CollaborationError("out_of_scope", "caller session cannot be nominated")
        if principal.elevated:
            project_id = principal.project_id or args.get("project_id")
            if args.get("project_id", project_id) != project_id:
                raise CollaborationError("out_of_scope", "a matching project scope is required")
            return {
                "kind": "supervisor",
                "project_id": project_id,
                "session_id": session.id,
                "instance_token": session.instance_token,
                "task_id": None,
                "claim_epoch": None,
                "created_by_id": f"supervisor-{session.project_id or 'global'}",
            }
        project_id = principal.project_id
        if (
            not project_id
            or session.project_id != project_id
            or args.get("project_id", project_id) != project_id
        ):
            raise CollaborationError("out_of_scope", "a matching project scope is required")
        if session.lifecycle not in ("task", "pool") or not session.task_id:
            raise CollaborationError("out_of_scope", "collaboration requires a held task")
        if args.get("task_id", session.task_id) != session.task_id:
            raise CollaborationError("out_of_scope", "caller task cannot be nominated")
        task = await self.db.get_task(session.task_id)
        if task is None or task.project_id != project_id:
            raise CollaborationError("out_of_scope", "session holds no task in this project")
        claim_epoch = args.get("claim_epoch")
        if mutation and session.lifecycle == "pool" and claim_epoch is None:
            raise CollaborationError(
                "stale_claim", "claim_epoch is required for pool collaboration mutations"
            )
        if (
            (claim_epoch is not None and claim_epoch != task.claim_epoch)
            or (
                session.last_claim_epoch is not None
                and session.last_claim_epoch != task.claim_epoch
            )
            or (session.lifecycle == "pool" and session.claim_phase != "active")
            or task.status != TaskStatus.IN_PROGRESS
            or not session.agent_id
            or task.assigned_agent_id != session.agent_id
        ):
            raise CollaborationError("stale_claim", "collaboration does not match the live claim")
        return {
            "kind": "member",
            "project_id": project_id,
            "session_id": session.id,
            "instance_token": session.instance_token,
            "task_id": task.id,
            "claim_epoch": task.claim_epoch,
            "created_by_id": None,
        }

    async def _visible_collaboration(self, caller: dict, thread_id: str) -> dict:
        """Return the thread the caller may read, else the uniform not-found."""
        project_id = caller["project_id"]
        if project_id is None and caller["kind"] != "member":
            # The operator and the global supervisor read any project's thread.
            project_id = await self.db.get_collaboration_thread_project(thread_id)
        thread = (
            await self.db.get_collaboration_thread(thread_id, project_id=project_id)
            if project_id
            else None
        )
        if thread is None or (
            caller["kind"] == "member"
            and not any(
                member["task_id"] == caller["task_id"] and member["state"] != "removed"
                for member in thread["members"]
            )
        ):
            raise CollaborationError("not_found", _NOT_FOUND)
        return thread

    async def _emit_collaboration_created(self, thread: dict) -> None:
        try:
            await self.orchestrator.bus.emit(
                "collaboration.created",
                {
                    "thread_id": thread["id"],
                    "project_id": thread["project_id"],
                    "task_ids": [member["task_id"] for member in thread["members"]],
                    "created_by_id": thread["created_by_id"],
                    "deadline_at": thread["deadline_at"],
                },
            )
        except Exception as exc:  # noqa: BLE001 - an event must never fail the command
            logger.warning("failed to emit collaboration.created: %s", exc)

    async def _cmd_collaboration_create(self, args):
        try:
            caller = await self._collaboration_caller(args, mutation=False)
            if caller["kind"] == "member":
                raise CollaborationError(
                    "out_of_scope", "only the operator or a supervisor creates collaborations"
                )
            values = CollaborationCreateArgs.model_validate(args)
            if self._messages_disabled_error():
                return {"success": False, "error": MESSAGES_DISABLED_ERROR}
            if not caller["project_id"]:
                raise CollaborationError("invalid", "project_id is required")
            now = time.time()
            thread = await self.db.create_collaboration_thread(
                project_id=caller["project_id"],
                created_by_kind=caller["kind"],
                created_by_id=caller["created_by_id"],
                idempotency_key=values.idempotency_key,
                task_ids=values.task_ids,
                goal=values.goal,
                deadline_seconds=values.deadline_seconds,
                message_budget=values.message_budget,
                now=now,
            )
            # The query stores ``now`` verbatim, so an older row is a replay.
            replayed = thread["created_at"] != now
            if not replayed:
                await self._emit_collaboration_created(thread)
            return {
                "success": True,
                "thread": _thread_view(thread, now),
                "replayed": replayed,
                "next_step": (
                    f"Each member accepts from its own task with "
                    f"`aq collaboration accept {thread['id']}`."
                ),
            }
        except CollaborationError as exc:
            return _refusal(exc)
        except (ValidationError, TypeError, ValueError) as exc:
            return _invalid(exc)

    async def _cmd_collaboration_accept(self, args):
        try:
            values = CollaborationAcceptArgs.model_validate(args)
            caller = await self._collaboration_caller(args, mutation=True)
            if caller["kind"] != "member":
                raise CollaborationError(
                    "out_of_scope", "only a member task's own session accepts a collaboration"
                )
            await self._visible_collaboration(caller, values.thread_id)
            now = time.time()
            try:
                thread = await self.db.accept_collaboration(
                    thread_id=values.thread_id,
                    project_id=caller["project_id"],
                    task_id=caller["task_id"],
                    claim_epoch=caller["claim_epoch"],
                    now=now,
                )
            except CollaborationError as exc:
                # A member removed since the visibility read reads as absent too.
                if exc.code == "collaboration.not_member":
                    raise CollaborationError("not_found", _NOT_FOUND) from exc
                raise
            return {
                "success": True,
                "thread": _thread_view(thread, now),
                "next_step": (
                    f"Joined for claim epoch {caller['claim_epoch']}. Read with "
                    f"`aq collaboration show {thread['id']}`."
                ),
            }
        except CollaborationError as exc:
            return _refusal(exc)
        except (ValidationError, TypeError, ValueError) as exc:
            return _invalid(exc)

    async def _cmd_collaboration_get(self, args):
        try:
            values = CollaborationGetArgs.model_validate(args)
            caller = await self._collaboration_caller(args, mutation=False)
            thread = await self._visible_collaboration(caller, values.thread_id)
            page = await self.db.read_collaboration_messages(
                thread["id"], project_id=thread["project_id"], after_seq=values.after_seq
            )
            if page is None:
                raise CollaborationError("not_found", _NOT_FOUND)
            now = time.time()
            view = _thread_view(thread, now)
            own = next(
                (m for m in view["members"] if m["task_id"] == caller["task_id"]), None
            )
            active = view["state"] == "active" and view["remaining_seconds"] > 0
            # A retained claim keeps its seat, so waiting on a partner that is
            # not running can strand every seat the partner would need.
            capacity_hold = bool(
                active
                and own is not None
                and own["running"]
                and not any(
                    m["running"]
                    for m in view["members"]
                    if m["task_id"] != own["task_id"] and m["state"] != "removed"
                )
            )
            thread_id, cursor = view["id"], page["next_cursor"]
            if not active:
                next_step = (
                    f"Thread {thread_id} is {view['state']}"
                    f" ({view['close_reason'] or 'deadline passed'}). Continue your own task."
                )
            elif own is not None and own["needs_accept"]:
                next_step = (
                    f"Run `aq collaboration accept {thread_id}` before sending or waiting."
                )
            elif capacity_hold:
                next_step = (
                    "No partner is running. Do not wait on this thread: your claim holds a "
                    "seat a partner may need. Prefer finishing your task or handing off."
                )
            elif own is not None:
                next_step = (
                    f"Send with `aq message send --thread-id {thread_id}`; wait with "
                    f"`aq message wait --thread {thread_id} --after {cursor or 0}`."
                )
            else:
                next_step = f"Close with `aq collaboration close {thread_id}` when it is done."
            return {
                "success": True,
                "thread": view,
                "messages": [_fields(CollaborationMessageRecord, m) for m in page["messages"]],
                "next_cursor": cursor,
                "has_more": page["has_more"],
                "capacity_hold": capacity_hold,
                "next_step": next_step,
            }
        except CollaborationError as exc:
            return _refusal(exc)
        except (ValidationError, TypeError, ValueError) as exc:
            return _invalid(exc)

    async def _cmd_collaboration_list(self, args):
        try:
            values = CollaborationListArgs.model_validate(args)
            caller = await self._collaboration_caller(args, mutation=False)
            project_id = caller["project_id"]
            task_id = caller["task_id"] if caller["kind"] == "member" else values.task_id
            if project_id is None and task_id is not None:
                task = await self.db.get_task(task_id)
                project_id = task.project_id if task else None
                if project_id is None:
                    return {"success": True, "threads": [], "count": 0}
            if project_id is None:
                raise CollaborationError("invalid", "project_id or task_id is required")
            rows = await self.db.list_collaboration_threads(
                project_id=project_id, task_id=task_id, state=values.state, limit=values.limit
            )
            now = time.time()
            threads = [_thread_view(row, now) for row in rows if row is not None]
            return {"success": True, "threads": threads, "count": len(threads)}
        except CollaborationError as exc:
            return _refusal(exc)
        except (ValidationError, TypeError, ValueError) as exc:
            return _invalid(exc)

    async def _cmd_collaboration_close(self, args):
        try:
            values = CollaborationCloseArgs.model_validate(args)
            caller = await self._collaboration_caller(args, mutation=True)
            elevated = caller["kind"] != "member"
            if (values.all_active or values.remove_task_id) and not elevated:
                raise CollaborationError(
                    "out_of_scope", "remove_task_id and all_active require an elevated caller"
                )
            now = time.time()
            if values.all_active:
                if values.thread_id or values.remove_task_id:
                    raise CollaborationError(
                        "invalid", "all_active takes no thread_id or remove_task_id"
                    )
                if not caller["project_id"]:
                    raise CollaborationError("invalid", "project_id is required")
                # The rollback lever (spec §5): stop every active thread and its
                # waits before membership enforcement is disabled.
                closed = []
                while True:
                    batch = await self.db.list_collaboration_threads(
                        project_id=caller["project_id"], state="active", limit=100
                    )
                    batch = [row for row in batch if row and row["id"] not in closed]
                    if not batch:
                        break
                    for row in batch:
                        await self.db.close_collaboration_thread(
                            thread_id=row["id"],
                            project_id=caller["project_id"],
                            reason="closed",
                            note=values.note,
                            now=now,
                        )
                        closed.append(row["id"])
                return {"success": True, "closed_count": len(closed), "thread_ids": closed}
            if not values.thread_id:
                raise CollaborationError("invalid", "thread_id is required")
            thread = await self._visible_collaboration(caller, values.thread_id)
            if values.remove_task_id:
                thread = await self.db.remove_collaboration_member(
                    thread_id=thread["id"],
                    project_id=thread["project_id"],
                    task_id=values.remove_task_id,
                    now=now,
                )
            else:
                # Closing a thread never closes, fails or abandons a member task.
                thread = await self.db.close_collaboration_thread(
                    thread_id=thread["id"],
                    project_id=thread["project_id"],
                    reason="closed",
                    note=values.note,
                    now=now,
                )
            return {"success": True, "thread": _thread_view(thread, now)}
        except CollaborationError as exc:
            return _refusal(exc)
        except (ValidationError, TypeError, ValueError) as exc:
            return _invalid(exc)
