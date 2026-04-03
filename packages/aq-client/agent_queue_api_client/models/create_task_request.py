from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateTaskRequest")


@_attrs_define
class CreateTaskRequest:
    """
    Attributes:
        title (str): Short task title
        project_id (None | str | Unset): Project ID (optional — inferred from active project)
        description (None | str | Unset): Detailed task description for the agent
        priority (int | Unset): Priority (lower = higher priority, default 100) Default: 100.
        requires_approval (bool | Unset): If true, agent work creates a PR instead of auto-merging. Human must
            approve/merge the PR. Default: False.
        task_type (None | str | Unset): Categorize the task type for display and filtering (optional)
        profile_id (None | str | Unset): Agent profile ID to configure the agent with specific tools/capabilities
            (optional)
        preferred_workspace_id (None | str | Unset): Workspace ID to prefer when assigning this task to an agent. Use
            this when the task must run in a specific workspace (e.g. one that contains a merge conflict). Get the ID from
            find_merge_conflict_workspaces or list_workspaces.
        attachments (list[Any] | None | Unset): List of absolute file paths to images or files that the agent should
            have access to when working on this task. These are typically paths to Discord attachment images that were
            downloaded locally. The agent will be told to read these files using the Read tool.
        auto_approve_plan (bool | Unset): If true, any plan this task generates will be automatically approved without
            waiting for human review. Default: False.
    """

    title: str
    project_id: None | str | Unset = UNSET
    description: None | str | Unset = UNSET
    priority: int | Unset = 100
    requires_approval: bool | Unset = False
    task_type: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    preferred_workspace_id: None | str | Unset = UNSET
    attachments: list[Any] | None | Unset = UNSET
    auto_approve_plan: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        title = self.title

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        priority = self.priority

        requires_approval = self.requires_approval

        task_type: None | str | Unset
        if isinstance(self.task_type, Unset):
            task_type = UNSET
        else:
            task_type = self.task_type

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        preferred_workspace_id: None | str | Unset
        if isinstance(self.preferred_workspace_id, Unset):
            preferred_workspace_id = UNSET
        else:
            preferred_workspace_id = self.preferred_workspace_id

        attachments: list[Any] | None | Unset
        if isinstance(self.attachments, Unset):
            attachments = UNSET
        elif isinstance(self.attachments, list):
            attachments = self.attachments

        else:
            attachments = self.attachments

        auto_approve_plan = self.auto_approve_plan

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "title": title,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if description is not UNSET:
            field_dict["description"] = description
        if priority is not UNSET:
            field_dict["priority"] = priority
        if requires_approval is not UNSET:
            field_dict["requires_approval"] = requires_approval
        if task_type is not UNSET:
            field_dict["task_type"] = task_type
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if preferred_workspace_id is not UNSET:
            field_dict["preferred_workspace_id"] = preferred_workspace_id
        if attachments is not UNSET:
            field_dict["attachments"] = attachments
        if auto_approve_plan is not UNSET:
            field_dict["auto_approve_plan"] = auto_approve_plan

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        title = d.pop("title")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        priority = d.pop("priority", UNSET)

        requires_approval = d.pop("requires_approval", UNSET)

        def _parse_task_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_type = _parse_task_type(d.pop("task_type", UNSET))

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_preferred_workspace_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        preferred_workspace_id = _parse_preferred_workspace_id(d.pop("preferred_workspace_id", UNSET))

        def _parse_attachments(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                attachments_type_0 = cast(list[Any], data)

                return attachments_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        attachments = _parse_attachments(d.pop("attachments", UNSET))

        auto_approve_plan = d.pop("auto_approve_plan", UNSET)

        create_task_request = cls(
            title=title,
            project_id=project_id,
            description=description,
            priority=priority,
            requires_approval=requires_approval,
            task_type=task_type,
            profile_id=profile_id,
            preferred_workspace_id=preferred_workspace_id,
            attachments=attachments,
            auto_approve_plan=auto_approve_plan,
        )

        create_task_request.additional_properties = d
        return create_task_request

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
