"""Recurring prompt response shapes."""

from typing import Any

from pydantic import BaseModel


class CronResponse(BaseModel):
    success: bool = True
    schedule: dict[str, Any]
    next_step: str | None = None


class CronListResponse(BaseModel):
    success: bool = True
    schedules: list[dict[str, Any]]
    count: int


RESPONSE_MODELS = {
    "cron_register": CronResponse,
    "cron_get": CronResponse,
    "cron_cancel": CronResponse,
    "cron_list": CronListResponse,
}
