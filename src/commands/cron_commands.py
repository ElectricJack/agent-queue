"""Recurring prompts derive their owner from the authenticated session."""

from __future__ import annotations

import time
import logging

from pydantic import ValidationError

from src.agent_cron import CronError, recurrence, visible_schedule
from src.commands.principal import PrincipalKind, current_principal

logger = logging.getLogger(__name__)


class CronCommandsMixin:
    async def _cron_identity(self, args):
        principal = current_principal()
        local = principal is None or principal.kind == PrincipalKind.LOCAL
        if not local and (principal.kind != PrincipalKind.SESSION or not principal.session_id):
            raise CronError("out_of_scope", "cron requires a live session owner")
        session_id = args.get("session_id") if local else principal.session_id
        session = await self.db.get_session(session_id) if session_id else None
        if not session or not session.instance_token:
            raise CronError("out_of_scope", "cron requires a live session owner")
        if not local and (
            principal.session_instance_token != session.instance_token
            or principal.project_id != session.project_id
        ):
            raise CronError("out_of_scope", "session instance or project scope does not match")
        if (
            args.get("session_id", session.id) != session.id
            or args.get("project_id", session.project_id) != session.project_id
            or args.get("task_id", session.task_id) != session.task_id
        ):
            raise CronError("out_of_scope", "cron cannot target another owner")
        return {
            "session_id": session.id,
            "instance_token": session.instance_token,
            "project_id": session.project_id,
            "claim_epoch": args.get("claim_epoch"),
            "elevated": local or principal.elevated,
        }

    @staticmethod
    def _cron_error(exc):
        return {
            "success": False,
            "error_code": getattr(exc, "code", "cron.invalid"),
            "error": str(exc),
        }

    async def _cmd_cron_register(self, args):
        try:
            from src.commands.contracts.cron import CronRegisterArgs

            values = CronRegisterArgs.model_validate(args)
            identity = await self._cron_identity(args)
            if not self.config.messages.enabled or not self.config.sessions.enabled:
                raise CronError("cron.disabled", "cron requires messages and sessions enabled")
            spec = recurrence(
                every=values.every,
                offset=values.offset,
                cron=values.cron,
                zone=values.timezone,
            )
            row = await self.db.register_agent_cron(
                identity=identity,
                prompt=values.prompt,
                recurrence=spec,
                key=values.idempotency_key,
                now=time.time(),
            )
            return {
                "success": True,
                "schedule": visible_schedule(row),
                "next_step": (
                    "Schedule registered for this session. The first future tick queues its prompt; "
                    "continue working or finish this turn. Reconnect with aq cron list --json."
                ),
            }
        except (CronError, ValidationError, ValueError, TypeError) as exc:
            return self._cron_error(exc)

    async def _cmd_cron_get(self, args):
        try:
            from src.commands.contracts.cron import CronGetArgs

            values = CronGetArgs.model_validate(args)
            identity = await self._cron_identity(args)
            row = await self.db.get_agent_cron(values.schedule_id)
            if not row:
                raise CronError("not_found", "schedule not found")
            if (
                row["session_id"] != identity["session_id"]
                or row["session_instance_token"] != identity["instance_token"]
                or row["project_id"] != identity["project_id"]
            ):
                raise CronError("out_of_scope", "schedule belongs to another owner instance")
            if values.consume:
                row = await self.db.mutate_agent_cron(
                    row["id"],
                    identity=identity,
                    now=time.time(),
                )
            return {"success": True, "schedule": visible_schedule(row)}
        except (CronError, ValidationError, ValueError, TypeError) as exc:
            return self._cron_error(exc)

    async def _cmd_cron_list(self, args):
        try:
            from src.commands.contracts.cron import CronListArgs

            values = CronListArgs.model_validate(args)
            identity = await self._cron_identity(args)
            rows = await self.db.list_agent_cron(
                session_id=identity["session_id"],
                instance_token=identity["instance_token"],
                limit=values.limit,
                offset=values.offset,
            )
            return {
                "success": True,
                "schedules": [visible_schedule(row) for row in rows],
                "count": len(rows),
            }
        except (CronError, ValidationError, ValueError, TypeError) as exc:
            return self._cron_error(exc)

    async def _cmd_cron_cancel(self, args):
        try:
            from src.commands.contracts.cron import CronPointerArgs

            values = CronPointerArgs.model_validate(args)
            identity = await self._cron_identity(args)
            row = await self.db.mutate_agent_cron(
                values.schedule_id,
                identity=identity,
                now=time.time(),
                cancel=True,
            )
            return {"success": True, "schedule": visible_schedule(row)}
        except (CronError, ValidationError, ValueError, TypeError) as exc:
            return self._cron_error(exc)

    @staticmethod
    def _cron_service_only():
        principal = current_principal()
        if (
            principal is None
            or principal.kind != PrincipalKind.SERVICE
            or principal.service_name != "agent-cron"
        ):
            raise CronError("out_of_scope", "only the cron daemon service may deliver schedules")

    async def _cmd_reconcile_agent_cron(self, args):
        try:
            self._cron_service_only()
            now = args.get("now")
            result = await self.db.reconcile_agent_cron(now=time.time() if now is None else now)
            for failure in result.pop("failures", []):
                logger.warning(
                    "Agent cron delivery failed for %s: %s",
                    failure["schedule_id"],
                    failure["error"],
                )
            return {"success": True, **result}
        except CronError as exc:
            return self._cron_error(exc)

    async def _cmd_cron_delivery_begin(self, args):
        try:
            self._cron_service_only()
            allowed = await self.db.begin_agent_cron_delivery(args["message_id"], now=time.time())
            return {"success": True, "allowed": allowed}
        except CronError as exc:
            return self._cron_error(exc)

    async def _cmd_cron_delivery_finish(self, args):
        try:
            self._cron_service_only()
            await self.db.finish_agent_cron_delivery(
                args["message_id"],
                now=time.time(),
                delivered=args["delivered"],
                error=args.get("error"),
            )
            return {"success": True}
        except CronError as exc:
            return self._cron_error(exc)
