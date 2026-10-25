"""Project schedule request and notification receipt responses."""

from typing import Any

from pydantic import BaseModel

from src.commands.contracts.schedule import (
    ScheduleListArgs,
    SchedulePointerArgs,
    ScheduleRegisterArgs,
)


class ScheduleResponse(BaseModel):
    success: bool = True
    schedule: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None


class ScheduleListResponse(BaseModel):
    success: bool = True
    schedules: list[dict[str, Any]] = []
    count: int = 0
    error: str | None = None
    error_code: str | None = None


REQUEST_MODELS = {
    "schedule_register": ScheduleRegisterArgs,
    "schedule_get": SchedulePointerArgs,
    "schedule_cancel": SchedulePointerArgs,
    "schedule_list": ScheduleListArgs,
}

RESPONSE_MODELS = {
    "schedule_register": ScheduleResponse,
    "schedule_get": ScheduleResponse,
    "schedule_cancel": ScheduleResponse,
    "schedule_list": ScheduleListResponse,
}
