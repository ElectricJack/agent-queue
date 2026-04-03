from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EditTaskRequest")


@_attrs_define
class EditTaskRequest:
    """
    Attributes:
        task_id (str): Task ID
        project_id (None | str | Unset): Move task to a different project (optional)
        title (None | str | Unset): New title (optional)
        description (None | str | Unset): New description (optional)
        priority (int | None | Unset): New priority (optional)
        task_type (None | str | Unset): New task type (optional, set to null to clear)
        status (None | str | Unset): New status — admin override, bypasses state machine (optional)
        max_retries (int | None | Unset): Max retry attempts (optional)
        verification_type (None | str | Unset): How to verify task output (optional)
        profile_id (None | str | Unset): Agent profile ID (optional, set to null to clear)
        auto_approve_plan (bool | None | Unset): If true, any plan this task generates will be automatically approved
            without human review (optional)
    """

    task_id: str
    project_id: None | str | Unset = UNSET
    title: None | str | Unset = UNSET
    description: None | str | Unset = UNSET
    priority: int | None | Unset = UNSET
    task_type: None | str | Unset = UNSET
    status: None | str | Unset = UNSET
    max_retries: int | None | Unset = UNSET
    verification_type: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    auto_approve_plan: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        priority: int | None | Unset
        if isinstance(self.priority, Unset):
            priority = UNSET
        else:
            priority = self.priority

        task_type: None | str | Unset
        if isinstance(self.task_type, Unset):
            task_type = UNSET
        else:
            task_type = self.task_type

        status: None | str | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        else:
            status = self.status

        max_retries: int | None | Unset
        if isinstance(self.max_retries, Unset):
            max_retries = UNSET
        else:
            max_retries = self.max_retries

        verification_type: None | str | Unset
        if isinstance(self.verification_type, Unset):
            verification_type = UNSET
        else:
            verification_type = self.verification_type

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        auto_approve_plan: bool | None | Unset
        if isinstance(self.auto_approve_plan, Unset):
            auto_approve_plan = UNSET
        else:
            auto_approve_plan = self.auto_approve_plan

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if title is not UNSET:
            field_dict["title"] = title
        if description is not UNSET:
            field_dict["description"] = description
        if priority is not UNSET:
            field_dict["priority"] = priority
        if task_type is not UNSET:
            field_dict["task_type"] = task_type
        if status is not UNSET:
            field_dict["status"] = status
        if max_retries is not UNSET:
            field_dict["max_retries"] = max_retries
        if verification_type is not UNSET:
            field_dict["verification_type"] = verification_type
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if auto_approve_plan is not UNSET:
            field_dict["auto_approve_plan"] = auto_approve_plan

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        def _parse_priority(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        priority = _parse_priority(d.pop("priority", UNSET))

        def _parse_task_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_type = _parse_task_type(d.pop("task_type", UNSET))

        def _parse_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        def _parse_max_retries(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_retries = _parse_max_retries(d.pop("max_retries", UNSET))

        def _parse_verification_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        verification_type = _parse_verification_type(d.pop("verification_type", UNSET))

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_auto_approve_plan(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        auto_approve_plan = _parse_auto_approve_plan(d.pop("auto_approve_plan", UNSET))

        edit_task_request = cls(
            task_id=task_id,
            project_id=project_id,
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            status=status,
            max_retries=max_retries,
            verification_type=verification_type,
            profile_id=profile_id,
            auto_approve_plan=auto_approve_plan,
        )

        edit_task_request.additional_properties = d
        return edit_task_request

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
