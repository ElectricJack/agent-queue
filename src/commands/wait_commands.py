"""Scoped durable waits. Owner identity is always derived on the server."""

from __future__ import annotations

import time

from pydantic import ValidationError

from src.agent_waits import WaitError, deadline_for, typed_match
from src.commands.principal import PrincipalKind, current_principal


def _error(code, message):
    return {"success": False, "error_code": code, "error": str(message)}


class WaitCommandsMixin:
    async def _wait_identity(self, args, *, mutation=False):
        principal = current_principal()
        if principal is None or principal.kind == PrincipalKind.LOCAL:
            if not mutation:
                return None
            # Operator registration still names a real session, never an arbitrary owner.
            session_id = args.get("session_id")
            session = await self.db.get_session(session_id) if session_id else None
            if not session:
                raise WaitError("out_of_scope", "register requires a live owner session")
            return dict(
                session_id=session.id,
                instance_token=session.instance_token,
                project_id=args.get("project_id") or session.project_id,
                claim_epoch=args.get("claim_epoch"),
                elevated=True,
            )
        if principal.kind != PrincipalKind.SESSION or not principal.session_id:
            raise WaitError("out_of_scope", "wait commands require a session owner")
        session = await self.db.get_session(principal.session_id)
        if not session or session.instance_token != principal.session_instance_token:
            raise WaitError("out_of_scope", "session instance does not match")
        project_id = principal.project_id or args.get("project_id")
        if not project_id or args.get("project_id", project_id) != project_id:
            raise WaitError("out_of_scope", "a matching project scope is required")
        if args.get("session_id", session.id) != session.id:
            raise WaitError("out_of_scope", "owner session cannot be nominated")
        if args.get("task_id", session.task_id) != session.task_id:
            raise WaitError("out_of_scope", "owner task cannot be nominated")
        return dict(
            session_id=session.id,
            instance_token=session.instance_token,
            project_id=project_id,
            claim_epoch=args.get("claim_epoch"),
            elevated=principal.elevated,
        )

    async def _wait_read_filter(self, args):
        principal = current_principal()
        if (
            principal is not None
            and principal.kind == PrincipalKind.SESSION
            and principal.elevated
            and principal.project_id is None
        ):
            session = await self.db.get_session(principal.session_id)
            if (
                session
                and session.lifecycle == "named"
                and session.profile_id == "supervisor"
                and session.project_id is None
                and session.instance_token == principal.session_instance_token
            ):
                return {"project_id": args.get("project_id")}
        identity = await self._wait_identity(args)
        if identity is None:
            return {"project_id": args.get("project_id")}
        session = await self.db.get_session(identity["session_id"])
        if (
            identity["elevated"]
            and session.lifecycle == "named"
            and session.profile_id == "supervisor"
        ):
            return {"project_id": identity["project_id"]}
        if not session.task_id or session.project_id != identity["project_id"]:
            raise WaitError("out_of_scope", "session has no task wait history")
        return dict(project_id=identity["project_id"], owner_kind="task", owner_id=session.task_id)

    async def _cmd_wait_register(self, args):
        try:
            from src.commands.contracts.wait import WaitRegisterArgs

            values = WaitRegisterArgs.model_validate(args)
            identity = await self._wait_identity(args, mutation=True)
            match = typed_match(values.kind, values.ref, values.after_seq, values.due_at)
            now = time.time()
            row = await self.db.register_agent_wait(
                identity=identity,
                kind=values.kind,
                match=match,
                deadline_at=deadline_for(now, values.timeout, match),
                idempotency_key=values.idempotency_key,
                now=now,
            )
            if row["owner_kind"] == "supervisor" and row["state"] == "active":
                return {
                    "success": True,
                    "wait": row,
                    "next_step": "Subscription registered. Continue working; its result will be queued.",
                }
            return {
                "success": True,
                "wait": row,
                "next_step": (
                    "End this turn. Resume from the result pointer with "
                    f"aq wait show {row['id']} --json. The claim, workspace and seat remain held."
                )
                if row["state"] == "active"
                else f"Read aq wait show {row['id']} --json.",
            }
        except WaitError as exc:
            return _error(exc.code, exc)
        except (ValidationError, TypeError, ValueError) as exc:
            return _error("wait.invalid", exc)

    async def _cmd_wait_get(self, args):
        try:
            filters = await self._wait_read_filter(args)
            row = await self.db.get_agent_wait(str(args.get("wait_id") or ""))
            if row is None:
                return _error("not_found", "wait not found")
            if any(value is not None and row[key] != value for key, value in filters.items()):
                return _error("out_of_scope", "wait history belongs to another owner or project")
            return {"success": True, "wait": row}
        except WaitError as exc:
            return _error(exc.code, exc)

    async def _cmd_wait_list(self, args):
        try:
            filters = await self._wait_read_filter(args)
            limit, offset = int(args.get("limit", 100)), int(args.get("offset", 0))
            if not 1 <= limit <= 100 or offset < 0:
                raise WaitError("wait.invalid", "limit must be 1–100 and offset nonnegative")
            rows = await self.db.list_agent_waits(**filters, limit=limit, offset=offset)
            return {"success": True, "waits": rows, "count": len(rows)}
        except (WaitError, ValueError, TypeError) as exc:
            return _error(getattr(exc, "code", "wait.invalid"), exc)

    async def _cmd_wait_cancel(self, args):
        try:
            principal = current_principal()
            if (
                principal is not None
                and principal.kind == PrincipalKind.SESSION
                and principal.elevated
                and principal.project_id is None
                and args.get("project_id") is None
            ):
                row = await self.db.get_agent_wait(str(args.get("wait_id") or ""))
                if row is None:
                    return _error("not_found", "wait not found")
                args = {**args, "project_id": row["project_id"]}
            identity = (
                None
                if principal is None or principal.kind == PrincipalKind.LOCAL
                else (await self._wait_identity(args, mutation=True))
            )
            row = await self.db.cancel_agent_wait(
                str(args.get("wait_id") or ""), identity=identity, now=time.time()
            )
            return {"success": True, "wait": row}
        except WaitError as exc:
            return _error(exc.code, exc)

    async def _cmd_reconcile_agent_waits(self, args):
        principal = current_principal()
        if principal is None or principal.kind != PrincipalKind.SERVICE:
            return _error("out_of_scope", "only the daemon may reconcile agent waits")
        now = args.get("now")
        result = await self.db.reconcile_agent_waits(
            now=time.time() if now is None else now, wait_id=args.get("wait_id")
        )
        return {"success": True, **result}
