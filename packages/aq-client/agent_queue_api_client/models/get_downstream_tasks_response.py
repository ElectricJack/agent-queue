from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.downstream_task import DownstreamTask


T = TypeVar("T", bound="GetDownstreamTasksResponse")


@_attrs_define
class GetDownstreamTasksResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        tasks (list[DownstreamTask] | Unset):
    """

    success: bool | Unset = True
    tasks: list[DownstreamTask] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        tasks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.tasks, Unset):
            tasks = []
            for tasks_item_data in self.tasks:
                tasks_item = tasks_item_data.to_dict()
                tasks.append(tasks_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if tasks is not UNSET:
            field_dict["tasks"] = tasks

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.downstream_task import DownstreamTask  # noqa: PLC0415

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _tasks = d.pop("tasks", UNSET)
        tasks: list[DownstreamTask] | Unset = UNSET
        if _tasks is not UNSET:
            tasks = []
            for tasks_item_data in _tasks:
                tasks_item = DownstreamTask.from_dict(tasks_item_data)

                tasks.append(tasks_item)

        get_downstream_tasks_response = cls(
            success=success,
            tasks=tasks,
        )

        get_downstream_tasks_response.additional_properties = d
        return get_downstream_tasks_response

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
