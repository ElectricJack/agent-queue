from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_status_summary_by_status import TaskStatusSummaryByStatus
    from ..models.task_status_summary_in_progress_item import TaskStatusSummaryInProgressItem
    from ..models.task_status_summary_ready_to_work_item import TaskStatusSummaryReadyToWorkItem


T = TypeVar("T", bound="TaskStatusSummary")


@_attrs_define
class TaskStatusSummary:
    """
    Attributes:
        total (int | Unset):  Default: 0.
        by_status (TaskStatusSummaryByStatus | Unset):
        in_progress (list[TaskStatusSummaryInProgressItem] | Unset):
        ready_to_work (list[TaskStatusSummaryReadyToWorkItem] | Unset):
    """

    total: int | Unset = 0
    by_status: TaskStatusSummaryByStatus | Unset = UNSET
    in_progress: list[TaskStatusSummaryInProgressItem] | Unset = UNSET
    ready_to_work: list[TaskStatusSummaryReadyToWorkItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        total = self.total

        by_status: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_status, Unset):
            by_status = self.by_status.to_dict()

        in_progress: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.in_progress, Unset):
            in_progress = []
            for in_progress_item_data in self.in_progress:
                in_progress_item = in_progress_item_data.to_dict()
                in_progress.append(in_progress_item)

        ready_to_work: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.ready_to_work, Unset):
            ready_to_work = []
            for ready_to_work_item_data in self.ready_to_work:
                ready_to_work_item = ready_to_work_item_data.to_dict()
                ready_to_work.append(ready_to_work_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if total is not UNSET:
            field_dict["total"] = total
        if by_status is not UNSET:
            field_dict["by_status"] = by_status
        if in_progress is not UNSET:
            field_dict["in_progress"] = in_progress
        if ready_to_work is not UNSET:
            field_dict["ready_to_work"] = ready_to_work

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_status_summary_by_status import TaskStatusSummaryByStatus
        from ..models.task_status_summary_in_progress_item import TaskStatusSummaryInProgressItem
        from ..models.task_status_summary_ready_to_work_item import TaskStatusSummaryReadyToWorkItem

        d = dict(src_dict)
        total = d.pop("total", UNSET)

        _by_status = d.pop("by_status", UNSET)
        by_status: TaskStatusSummaryByStatus | Unset
        if isinstance(_by_status, Unset):
            by_status = UNSET
        else:
            by_status = TaskStatusSummaryByStatus.from_dict(_by_status)

        _in_progress = d.pop("in_progress", UNSET)
        in_progress: list[TaskStatusSummaryInProgressItem] | Unset = UNSET
        if _in_progress is not UNSET:
            in_progress = []
            for in_progress_item_data in _in_progress:
                in_progress_item = TaskStatusSummaryInProgressItem.from_dict(in_progress_item_data)

                in_progress.append(in_progress_item)

        _ready_to_work = d.pop("ready_to_work", UNSET)
        ready_to_work: list[TaskStatusSummaryReadyToWorkItem] | Unset = UNSET
        if _ready_to_work is not UNSET:
            ready_to_work = []
            for ready_to_work_item_data in _ready_to_work:
                ready_to_work_item = TaskStatusSummaryReadyToWorkItem.from_dict(ready_to_work_item_data)

                ready_to_work.append(ready_to_work_item)

        task_status_summary = cls(
            total=total,
            by_status=by_status,
            in_progress=in_progress,
            ready_to_work=ready_to_work,
        )

        task_status_summary.additional_properties = d
        return task_status_summary

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
