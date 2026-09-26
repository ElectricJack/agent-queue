"""Concrete job request and response models shared with public contracts."""

from typing import Any
from pydantic import BaseModel

from src.commands.contracts.job import (
    JobSubmitArgs,
    JobGetArgs,
    JobListArgs,
    JobResultArgs,
    JobLogsArgs,
    JobValue,
    JobListValue,
    JobResultValue,
    JobLogsValue,
)


class JobResponse(JobValue):
    success: bool = True


class JobListResponse(JobListValue):
    success: bool = True


class JobResultResponse(JobResultValue):
    success: bool = True


class JobLogsResponse(JobLogsValue):
    success: bool = True


class JobErrorResponse(BaseModel):
    success: bool = False
    error: str
    error_code: str | None = None
    result: dict[str, Any] | None = None


REQUEST_MODELS = {
    "job_submit": JobSubmitArgs,
    "job_get": JobGetArgs,
    "job_list": JobListArgs,
    "job_cancel": JobGetArgs,
    "job_result": JobResultArgs,
    "job_logs": JobLogsArgs,
}
RESPONSE_MODELS = {
    "job_submit": JobResponse,
    "job_get": JobResponse,
    "job_list": JobListResponse,
    "job_cancel": JobResponse,
    "job_result": JobResultResponse,
    "job_logs": JobLogsResponse,
}
