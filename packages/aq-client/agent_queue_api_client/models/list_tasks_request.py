from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ListTasksRequest")


@_attrs_define
class ListTasksRequest:
    """
    Attributes:
        project_id (None | str | Unset): Filter by project ID (optional, but required for tree/compact modes)
        status (None | str | Unset): Filter by exact status: DEFINED, READY, IN_PROGRESS, COMPLETED, etc. When provided,
            show_all, include_completed, and completed_only are ignored.
        show_all (bool | None | Unset): When true, return ALL tasks regardless of status (active + completed + failed +
            blocked). Default false (only active tasks are shown).
        include_completed (bool | None | Unset): When true, return all tasks including completed/failed/blocked. Alias
            for show_all. Default false.
        completed_only (bool | None | Unset): When true, return ONLY completed/failed/blocked tasks. Default false.
        display_mode (None | str | Unset): How to format the task list. 'flat' (default) returns a JSON array of task
            objects. 'tree' returns a hierarchical view using box-drawing characters showing parent/subtask relationships —
            ideal for plans with subtasks. 'compact' returns each root task with a progress bar summarizing subtask
            completion. When display_mode is 'tree' or 'compact', the response includes a 'display' field with pre-formatted
            text.
        show_dependencies (bool | None | Unset): When true, each task in the result includes 'depends_on' (list of
            upstream tasks with id and status) and 'blocks' (list of downstream task IDs). Also adds a 'dependency_display'
            field with pre-formatted text showing dependency relationships. Use this when the user asks about task
            dependencies, blocking chains, or why a task is waiting. Default false.
        pagination (None | str | Unset):
        snapshot_id (None | str | Unset):
        page_token (None | str | Unset):
        page_size (int | None | Unset):
    """

    project_id: None | str | Unset = UNSET
    status: None | str | Unset = UNSET
    show_all: bool | None | Unset = UNSET
    include_completed: bool | None | Unset = UNSET
    completed_only: bool | None | Unset = UNSET
    display_mode: None | str | Unset = UNSET
    show_dependencies: bool | None | Unset = UNSET
    pagination: None | str | Unset = UNSET
    snapshot_id: None | str | Unset = UNSET
    page_token: None | str | Unset = UNSET
    page_size: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        status: None | str | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        else:
            status = self.status

        show_all: bool | None | Unset
        if isinstance(self.show_all, Unset):
            show_all = UNSET
        else:
            show_all = self.show_all

        include_completed: bool | None | Unset
        if isinstance(self.include_completed, Unset):
            include_completed = UNSET
        else:
            include_completed = self.include_completed

        completed_only: bool | None | Unset
        if isinstance(self.completed_only, Unset):
            completed_only = UNSET
        else:
            completed_only = self.completed_only

        display_mode: None | str | Unset
        if isinstance(self.display_mode, Unset):
            display_mode = UNSET
        else:
            display_mode = self.display_mode

        show_dependencies: bool | None | Unset
        if isinstance(self.show_dependencies, Unset):
            show_dependencies = UNSET
        else:
            show_dependencies = self.show_dependencies

        pagination: None | str | Unset
        if isinstance(self.pagination, Unset):
            pagination = UNSET
        else:
            pagination = self.pagination

        snapshot_id: None | str | Unset
        if isinstance(self.snapshot_id, Unset):
            snapshot_id = UNSET
        else:
            snapshot_id = self.snapshot_id

        page_token: None | str | Unset
        if isinstance(self.page_token, Unset):
            page_token = UNSET
        else:
            page_token = self.page_token

        page_size: int | None | Unset
        if isinstance(self.page_size, Unset):
            page_size = UNSET
        else:
            page_size = self.page_size

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if status is not UNSET:
            field_dict["status"] = status
        if show_all is not UNSET:
            field_dict["show_all"] = show_all
        if include_completed is not UNSET:
            field_dict["include_completed"] = include_completed
        if completed_only is not UNSET:
            field_dict["completed_only"] = completed_only
        if display_mode is not UNSET:
            field_dict["display_mode"] = display_mode
        if show_dependencies is not UNSET:
            field_dict["show_dependencies"] = show_dependencies
        if pagination is not UNSET:
            field_dict["pagination"] = pagination
        if snapshot_id is not UNSET:
            field_dict["snapshot_id"] = snapshot_id
        if page_token is not UNSET:
            field_dict["page_token"] = page_token
        if page_size is not UNSET:
            field_dict["page_size"] = page_size

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        def _parse_show_all(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        show_all = _parse_show_all(d.pop("show_all", UNSET))

        def _parse_include_completed(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        include_completed = _parse_include_completed(d.pop("include_completed", UNSET))

        def _parse_completed_only(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        completed_only = _parse_completed_only(d.pop("completed_only", UNSET))

        def _parse_display_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        display_mode = _parse_display_mode(d.pop("display_mode", UNSET))

        def _parse_show_dependencies(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        show_dependencies = _parse_show_dependencies(d.pop("show_dependencies", UNSET))

        def _parse_pagination(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pagination = _parse_pagination(d.pop("pagination", UNSET))

        def _parse_snapshot_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        snapshot_id = _parse_snapshot_id(d.pop("snapshot_id", UNSET))

        def _parse_page_token(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        page_token = _parse_page_token(d.pop("page_token", UNSET))

        def _parse_page_size(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        page_size = _parse_page_size(d.pop("page_size", UNSET))

        list_tasks_request = cls(
            project_id=project_id,
            status=status,
            show_all=show_all,
            include_completed=include_completed,
            completed_only=completed_only,
            display_mode=display_mode,
            show_dependencies=show_dependencies,
            pagination=pagination,
            snapshot_id=snapshot_id,
            page_token=page_token,
            page_size=page_size,
        )

        list_tasks_request.additional_properties = d
        return list_tasks_request

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
