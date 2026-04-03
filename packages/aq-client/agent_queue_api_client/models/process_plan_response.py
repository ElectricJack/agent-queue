from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProcessPlanResponse")


@_attrs_define
class ProcessPlanResponse:
    """
    Attributes:
        status (str | Unset):  Default: ''.
        project_id (str | Unset):  Default: ''.
        task_id (None | str | Unset):
        plan_path (None | str | Unset):
        title (None | str | Unset):
        phases (int | None | Unset):
        draft_subtasks (int | None | Unset):
        total_plan_files_found (int | None | Unset):
        workspaces_scanned (int | None | Unset):
        message (None | str | Unset):
        note (None | str | Unset):
    """

    status: str | Unset = ""
    project_id: str | Unset = ""
    task_id: None | str | Unset = UNSET
    plan_path: None | str | Unset = UNSET
    title: None | str | Unset = UNSET
    phases: int | None | Unset = UNSET
    draft_subtasks: int | None | Unset = UNSET
    total_plan_files_found: int | None | Unset = UNSET
    workspaces_scanned: int | None | Unset = UNSET
    message: None | str | Unset = UNSET
    note: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        status = self.status

        project_id = self.project_id

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        plan_path: None | str | Unset
        if isinstance(self.plan_path, Unset):
            plan_path = UNSET
        else:
            plan_path = self.plan_path

        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title

        phases: int | None | Unset
        if isinstance(self.phases, Unset):
            phases = UNSET
        else:
            phases = self.phases

        draft_subtasks: int | None | Unset
        if isinstance(self.draft_subtasks, Unset):
            draft_subtasks = UNSET
        else:
            draft_subtasks = self.draft_subtasks

        total_plan_files_found: int | None | Unset
        if isinstance(self.total_plan_files_found, Unset):
            total_plan_files_found = UNSET
        else:
            total_plan_files_found = self.total_plan_files_found

        workspaces_scanned: int | None | Unset
        if isinstance(self.workspaces_scanned, Unset):
            workspaces_scanned = UNSET
        else:
            workspaces_scanned = self.workspaces_scanned

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        note: None | str | Unset
        if isinstance(self.note, Unset):
            note = UNSET
        else:
            note = self.note

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if status is not UNSET:
            field_dict["status"] = status
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if plan_path is not UNSET:
            field_dict["plan_path"] = plan_path
        if title is not UNSET:
            field_dict["title"] = title
        if phases is not UNSET:
            field_dict["phases"] = phases
        if draft_subtasks is not UNSET:
            field_dict["draft_subtasks"] = draft_subtasks
        if total_plan_files_found is not UNSET:
            field_dict["total_plan_files_found"] = total_plan_files_found
        if workspaces_scanned is not UNSET:
            field_dict["workspaces_scanned"] = workspaces_scanned
        if message is not UNSET:
            field_dict["message"] = message
        if note is not UNSET:
            field_dict["note"] = note

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        status = d.pop("status", UNSET)

        project_id = d.pop("project_id", UNSET)

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_plan_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        plan_path = _parse_plan_path(d.pop("plan_path", UNSET))

        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))

        def _parse_phases(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        phases = _parse_phases(d.pop("phases", UNSET))

        def _parse_draft_subtasks(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        draft_subtasks = _parse_draft_subtasks(d.pop("draft_subtasks", UNSET))

        def _parse_total_plan_files_found(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        total_plan_files_found = _parse_total_plan_files_found(d.pop("total_plan_files_found", UNSET))

        def _parse_workspaces_scanned(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        workspaces_scanned = _parse_workspaces_scanned(d.pop("workspaces_scanned", UNSET))

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        def _parse_note(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        note = _parse_note(d.pop("note", UNSET))

        process_plan_response = cls(
            status=status,
            project_id=project_id,
            task_id=task_id,
            plan_path=plan_path,
            title=title,
            phases=phases,
            draft_subtasks=draft_subtasks,
            total_plan_files_found=total_plan_files_found,
            workspaces_scanned=workspaces_scanned,
            message=message,
            note=note,
        )

        process_plan_response.additional_properties = d
        return process_plan_response

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
