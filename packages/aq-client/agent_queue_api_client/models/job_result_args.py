from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="JobResultArgs")


@_attrs_define
class JobResultArgs:
    """
    Attributes:
        job_id (str):
        max_bytes (int | Unset):  Default: 8192.
    """

    job_id: str
    max_bytes: int | Unset = 8192

    def to_dict(self) -> dict[str, Any]:
        job_id = self.job_id

        max_bytes = self.max_bytes

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "job_id": job_id,
            }
        )
        if max_bytes is not UNSET:
            field_dict["max_bytes"] = max_bytes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        job_id = d.pop("job_id")

        max_bytes = d.pop("max_bytes", UNSET)

        job_result_args = cls(
            job_id=job_id,
            max_bytes=max_bytes,
        )

        return job_result_args
