from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.job_list_response_jobs_item import JobListResponseJobsItem


T = TypeVar("T", bound="JobListResponse")


@_attrs_define
class JobListResponse:
    """
    Attributes:
        jobs (list[JobListResponseJobsItem]):
        success (bool | Unset):  Default: True.
    """

    jobs: list[JobListResponseJobsItem]
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        jobs = []
        for jobs_item_data in self.jobs:
            jobs_item = jobs_item_data.to_dict()
            jobs.append(jobs_item)

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "jobs": jobs,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.job_list_response_jobs_item import JobListResponseJobsItem

        d = dict(src_dict)
        jobs = []
        _jobs = d.pop("jobs")
        for jobs_item_data in _jobs:
            jobs_item = JobListResponseJobsItem.from_dict(jobs_item_data)

            jobs.append(jobs_item)

        success = d.pop("success", UNSET)

        job_list_response = cls(
            jobs=jobs,
            success=success,
        )

        return job_list_response
