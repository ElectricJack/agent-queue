"""Project schedules belong to operators/supervisors, never held workers."""

from __future__ import annotations

import time

from pydantic import ValidationError

from src.commands.contracts.schedule import (
    ScheduleListArgs,
    SchedulePointerArgs,
    ScheduleRegisterArgs,
)
from src.commands.principal import PrincipalKind, current_principal
from src.project_schedules import ScheduleError, utc_instant, visible_schedule


class ScheduleCommandsMixin:
    async def _schedule_project(self, project_id):
        principal = current_principal()
        if principal is not None and principal.kind != PrincipalKind.LOCAL:
            if principal.kind != PrincipalKind.SESSION or not principal.elevated:
                raise ScheduleError("out_of_scope", "project schedules require a supervisor")
            if principal.project_id is not None and principal.project_id != project_id:
                raise ScheduleError("out_of_scope", "project scope does not match")
        if await self.db.get_project(project_id) is None:
            raise ScheduleError("schedule.not_found", "project not found")

    @staticmethod
    def _schedule_error(exc):
        return {
            "success": False, "error_code": getattr(exc, "code", "schedule.invalid"),
            "error": str(exc),
        }

    async def _cmd_schedule_register(self, args):
        try:
            values = ScheduleRegisterArgs.model_validate(args)
            await self._schedule_project(values.project_id)
            row = await self.db.register_project_schedule(
                project_id=values.project_id, due_at=utc_instant(values.due_at),
                body=values.body, key=values.idempotency_key, now=time.time(),
            )
            return {"success": True, "schedule": visible_schedule(row)}
        except (ScheduleError, ValidationError) as exc:
            return self._schedule_error(exc)

    async def _cmd_schedule_get(self, args):
        try:
            values = SchedulePointerArgs.model_validate(args)
            await self._schedule_project(values.project_id)
            row = await self.db.get_project_schedule(
                project_id=values.project_id, schedule_id=values.schedule_id,
            )
            return {"success": True, "schedule": visible_schedule(row)}
        except (ScheduleError, ValidationError) as exc:
            return self._schedule_error(exc)

    async def _cmd_schedule_list(self, args):
        try:
            values = ScheduleListArgs.model_validate(args)
            await self._schedule_project(values.project_id)
            rows = await self.db.list_project_schedules(**values.model_dump())
            return {
                "success": True, "schedules": [visible_schedule(row) for row in rows],
                "count": len(rows),
            }
        except (ScheduleError, ValidationError) as exc:
            return self._schedule_error(exc)

    async def _cmd_schedule_cancel(self, args):
        try:
            values = SchedulePointerArgs.model_validate(args)
            await self._schedule_project(values.project_id)
            row = await self.db.cancel_project_schedule(**values.model_dump(), now=time.time())
            return {"success": True, "schedule": visible_schedule(row)}
        except (ScheduleError, ValidationError) as exc:
            return self._schedule_error(exc)

    async def _cmd_reconcile_project_schedules(self, args):
        principal = current_principal()
        if (principal is None or principal.kind != PrincipalKind.SERVICE
                or principal.service_name != "project-schedules"):
            return self._schedule_error(ScheduleError("out_of_scope", "daemon service only"))
        fired = await self.db.fire_project_schedules(now=args["now"])
        return {"success": True, "fired": fired}
