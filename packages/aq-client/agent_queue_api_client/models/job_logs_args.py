from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="JobLogsArgs")


@_attrs_define
class JobLogsArgs:
    """
    Attributes:
        job_id (str):
        after (int | Unset):  Default: 0.
        limit (int | Unset):  Default: 65536.
    """

    job_id: str
    after: int | Unset = 0
    limit: int | Unset = 65536

    def to_dict(self) -> dict[str, Any]:
        job_id = self.job_id

        after = self.after

        limit = self.limit

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "job_id": job_id,
            }
        )
        if after is not UNSET:
            field_dict["after"] = after
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        job_id = d.pop("job_id")

        after = d.pop("after", UNSET)

        limit = d.pop("limit", UNSET)

        job_logs_args = cls(
            job_id=job_id,
            after=after,
            limit=limit,
        )

        return job_logs_args
