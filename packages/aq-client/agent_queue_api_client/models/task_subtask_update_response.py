from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_subtask_with_context import TaskSubtaskWithContext


T = TypeVar("T", bound="TaskSubtaskUpdateResponse")


@_attrs_define
class TaskSubtaskUpdateResponse:
    """
    Attributes:
        subtask (TaskSubtaskWithContext):
        total (int):
        settled (int):
        success (bool | Unset):  Default: True.
    """

    subtask: TaskSubtaskWithContext
    total: int
    settled: int
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        subtask = self.subtask.to_dict()

        total = self.total

        settled = self.settled

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "subtask": subtask,
                "total": total,
                "settled": settled,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_subtask_with_context import TaskSubtaskWithContext

        d = dict(src_dict)
        subtask = TaskSubtaskWithContext.from_dict(d.pop("subtask"))

        total = d.pop("total")

        settled = d.pop("settled")

        success = d.pop("success", UNSET)

        task_subtask_update_response = cls(
            subtask=subtask,
            total=total,
            settled=settled,
            success=success,
        )

        task_subtask_update_response.additional_properties = d
        return task_subtask_update_response

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
