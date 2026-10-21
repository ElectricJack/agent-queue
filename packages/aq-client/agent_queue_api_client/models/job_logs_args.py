from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..models.job_logs_args_stream import JobLogsArgsStream
from ..types import UNSET, Unset

T = TypeVar("T", bound="JobLogsArgs")


@_attrs_define
class JobLogsArgs:
    """
    Attributes:
        job_id (str):
        stream (JobLogsArgsStream | Unset):  Default: JobLogsArgsStream.STDOUT.
        after (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 65536.
    """

    job_id: str
    stream: JobLogsArgsStream | Unset = JobLogsArgsStream.STDOUT
    after: int | Unset = 0
    limit: int | Unset = 65536

    def to_dict(self) -> dict[str, Any]:
        job_id = self.job_id

        stream: str | Unset = UNSET
        if not isinstance(self.stream, Unset):
            stream = self.stream.value

        after = self.after

        limit = self.limit

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "job_id": job_id,
            }
        )
        if stream is not UNSET:
            field_dict["stream"] = stream
        if after is not UNSET:
            field_dict["after"] = after
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        job_id = d.pop("job_id")

        _stream = d.pop("stream", UNSET)
        stream: JobLogsArgsStream | Unset
        if isinstance(_stream, Unset):
            stream = UNSET
        else:
            stream = JobLogsArgsStream(_stream)

        after = d.pop("after", UNSET)

        limit = d.pop("limit", UNSET)

        job_logs_args = cls(
            job_id=job_id,
            stream=stream,
            after=after,
            limit=limit,
        )

        return job_logs_args
