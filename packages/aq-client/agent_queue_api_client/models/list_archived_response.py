from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.list_archived_response_tasks_item import ListArchivedResponseTasksItem


T = TypeVar("T", bound="ListArchivedResponse")


@_attrs_define
class ListArchivedResponse:
    """
    Attributes:
        tasks (list[ListArchivedResponseTasksItem] | Unset):
        count (int | Unset):  Default: 0.
        total (int | Unset):  Default: 0.
        project_id (None | str | Unset):
    """

    tasks: list[ListArchivedResponseTasksItem] | Unset = UNSET
    count: int | Unset = 0
    total: int | Unset = 0
    project_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        tasks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.tasks, Unset):
            tasks = []
            for tasks_item_data in self.tasks:
                tasks_item = tasks_item_data.to_dict()
                tasks.append(tasks_item)

        count = self.count

        total = self.total

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if tasks is not UNSET:
            field_dict["tasks"] = tasks
        if count is not UNSET:
            field_dict["count"] = count
        if total is not UNSET:
            field_dict["total"] = total
        if project_id is not UNSET:
            field_dict["project_id"] = project_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.list_archived_response_tasks_item import ListArchivedResponseTasksItem

        d = dict(src_dict)
        _tasks = d.pop("tasks", UNSET)
        tasks: list[ListArchivedResponseTasksItem] | Unset = UNSET
        if _tasks is not UNSET:
            tasks = []
            for tasks_item_data in _tasks:
                tasks_item = ListArchivedResponseTasksItem.from_dict(tasks_item_data)

                tasks.append(tasks_item)

        count = d.pop("count", UNSET)

        total = d.pop("total", UNSET)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        list_archived_response = cls(
            tasks=tasks,
            count=count,
            total=total,
            project_id=project_id,
        )

        list_archived_response.additional_properties = d
        return list_archived_response

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
