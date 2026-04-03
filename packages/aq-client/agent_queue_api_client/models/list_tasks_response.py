from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.list_tasks_response_tasks_item import ListTasksResponseTasksItem


T = TypeVar("T", bound="ListTasksResponse")


@_attrs_define
class ListTasksResponse:
    """
    Attributes:
        display_mode (str | Unset):  Default: 'flat'.
        tasks (list[ListTasksResponseTasksItem] | Unset):
        total (int | Unset):  Default: 0.
        hidden_completed (int | Unset):  Default: 0.
        filtered (bool | Unset):  Default: False.
        dependency_display (None | str | Unset):
    """

    display_mode: str | Unset = "flat"
    tasks: list[ListTasksResponseTasksItem] | Unset = UNSET
    total: int | Unset = 0
    hidden_completed: int | Unset = 0
    filtered: bool | Unset = False
    dependency_display: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        display_mode = self.display_mode

        tasks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.tasks, Unset):
            tasks = []
            for tasks_item_data in self.tasks:
                tasks_item = tasks_item_data.to_dict()
                tasks.append(tasks_item)

        total = self.total

        hidden_completed = self.hidden_completed

        filtered = self.filtered

        dependency_display: None | str | Unset
        if isinstance(self.dependency_display, Unset):
            dependency_display = UNSET
        else:
            dependency_display = self.dependency_display

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if display_mode is not UNSET:
            field_dict["display_mode"] = display_mode
        if tasks is not UNSET:
            field_dict["tasks"] = tasks
        if total is not UNSET:
            field_dict["total"] = total
        if hidden_completed is not UNSET:
            field_dict["hidden_completed"] = hidden_completed
        if filtered is not UNSET:
            field_dict["filtered"] = filtered
        if dependency_display is not UNSET:
            field_dict["dependency_display"] = dependency_display

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.list_tasks_response_tasks_item import ListTasksResponseTasksItem

        d = dict(src_dict)
        display_mode = d.pop("display_mode", UNSET)

        _tasks = d.pop("tasks", UNSET)
        tasks: list[ListTasksResponseTasksItem] | Unset = UNSET
        if _tasks is not UNSET:
            tasks = []
            for tasks_item_data in _tasks:
                tasks_item = ListTasksResponseTasksItem.from_dict(tasks_item_data)

                tasks.append(tasks_item)

        total = d.pop("total", UNSET)

        hidden_completed = d.pop("hidden_completed", UNSET)

        filtered = d.pop("filtered", UNSET)

        def _parse_dependency_display(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        dependency_display = _parse_dependency_display(d.pop("dependency_display", UNSET))

        list_tasks_response = cls(
            display_mode=display_mode,
            tasks=tasks,
            total=total,
            hidden_completed=hidden_completed,
            filtered=filtered,
            dependency_display=dependency_display,
        )

        list_tasks_response.additional_properties = d
        return list_tasks_response

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
