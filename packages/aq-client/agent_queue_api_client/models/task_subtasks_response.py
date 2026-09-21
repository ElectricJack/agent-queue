from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_subtask import TaskSubtask


T = TypeVar("T", bound="TaskSubtasksResponse")


@_attrs_define
class TaskSubtasksResponse:
    """
    Attributes:
        task_id (str):
        total (int):
        settled (int):
        success (bool | Unset):  Default: True.
        subtasks (list[TaskSubtask] | Unset):
    """

    task_id: str
    total: int
    settled: int
    success: bool | Unset = True
    subtasks: list[TaskSubtask] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        total = self.total

        settled = self.settled

        success = self.success

        subtasks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.subtasks, Unset):
            subtasks = []
            for subtasks_item_data in self.subtasks:
                subtasks_item = subtasks_item_data.to_dict()
                subtasks.append(subtasks_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "total": total,
                "settled": settled,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if subtasks is not UNSET:
            field_dict["subtasks"] = subtasks

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_subtask import TaskSubtask

        d = dict(src_dict)
        task_id = d.pop("task_id")

        total = d.pop("total")

        settled = d.pop("settled")

        success = d.pop("success", UNSET)

        _subtasks = d.pop("subtasks", UNSET)
        subtasks: list[TaskSubtask] | Unset = UNSET
        if _subtasks is not UNSET:
            subtasks = []
            for subtasks_item_data in _subtasks:
                subtasks_item = TaskSubtask.from_dict(subtasks_item_data)

                subtasks.append(subtasks_item)

        task_subtasks_response = cls(
            task_id=task_id,
            total=total,
            settled=settled,
            success=success,
            subtasks=subtasks,
        )

        task_subtasks_response.additional_properties = d
        return task_subtasks_response

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
