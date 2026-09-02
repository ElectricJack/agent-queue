from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.list_active_tasks_all_projects_response_by_project import ListActiveTasksAllProjectsResponseByProject
    from ..models.list_active_tasks_all_projects_response_tasks_item import ListActiveTasksAllProjectsResponseTasksItem


T = TypeVar("T", bound="ListActiveTasksAllProjectsResponse")


@_attrs_define
class ListActiveTasksAllProjectsResponse:
    """
    Attributes:
        by_project (ListActiveTasksAllProjectsResponseByProject | Unset):
        tasks (list[ListActiveTasksAllProjectsResponseTasksItem] | Unset):
        total (int | Unset):  Default: 0.
        project_count (int | Unset):  Default: 0.
        hidden_completed (int | Unset):  Default: 0.
    """

    by_project: ListActiveTasksAllProjectsResponseByProject | Unset = UNSET
    tasks: list[ListActiveTasksAllProjectsResponseTasksItem] | Unset = UNSET
    total: int | Unset = 0
    project_count: int | Unset = 0
    hidden_completed: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        by_project: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_project, Unset):
            by_project = self.by_project.to_dict()

        tasks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.tasks, Unset):
            tasks = []
            for tasks_item_data in self.tasks:
                tasks_item = tasks_item_data.to_dict()
                tasks.append(tasks_item)

        total = self.total

        project_count = self.project_count

        hidden_completed = self.hidden_completed

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if by_project is not UNSET:
            field_dict["by_project"] = by_project
        if tasks is not UNSET:
            field_dict["tasks"] = tasks
        if total is not UNSET:
            field_dict["total"] = total
        if project_count is not UNSET:
            field_dict["project_count"] = project_count
        if hidden_completed is not UNSET:
            field_dict["hidden_completed"] = hidden_completed

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.list_active_tasks_all_projects_response_by_project import (
            ListActiveTasksAllProjectsResponseByProject,
        )
        from ..models.list_active_tasks_all_projects_response_tasks_item import (
            ListActiveTasksAllProjectsResponseTasksItem,
        )

        d = dict(src_dict)
        _by_project = d.pop("by_project", UNSET)
        by_project: ListActiveTasksAllProjectsResponseByProject | Unset
        if isinstance(_by_project, Unset):
            by_project = UNSET
        else:
            by_project = ListActiveTasksAllProjectsResponseByProject.from_dict(_by_project)

        _tasks = d.pop("tasks", UNSET)
        tasks: list[ListActiveTasksAllProjectsResponseTasksItem] | Unset = UNSET
        if _tasks is not UNSET:
            tasks = []
            for tasks_item_data in _tasks:
                tasks_item = ListActiveTasksAllProjectsResponseTasksItem.from_dict(tasks_item_data)

                tasks.append(tasks_item)

        total = d.pop("total", UNSET)

        project_count = d.pop("project_count", UNSET)

        hidden_completed = d.pop("hidden_completed", UNSET)

        list_active_tasks_all_projects_response = cls(
            by_project=by_project,
            tasks=tasks,
            total=total,
            project_count=project_count,
            hidden_completed=hidden_completed,
        )

        list_active_tasks_all_projects_response.additional_properties = d
        return list_active_tasks_all_projects_response

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
