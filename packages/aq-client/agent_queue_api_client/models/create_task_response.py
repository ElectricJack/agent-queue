from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateTaskResponse")


@_attrs_define
class CreateTaskResponse:
    """
    Attributes:
        created (str):
        title (str):
        project_id (str):
        requires_approval (bool | Unset):  Default: False.
        task_type (None | str | Unset):
        profile_id (None | str | Unset):
        preferred_workspace_id (None | str | Unset):
        attachments (list[str] | None | Unset):
        auto_approve_plan (bool | Unset):  Default: False.
        warning (None | str | Unset):
    """

    created: str
    title: str
    project_id: str
    requires_approval: bool | Unset = False
    task_type: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    preferred_workspace_id: None | str | Unset = UNSET
    attachments: list[str] | None | Unset = UNSET
    auto_approve_plan: bool | Unset = False
    warning: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        title = self.title

        project_id = self.project_id

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

        attachments: list[str] | None | Unset
        if isinstance(self.attachments, Unset):
            attachments = UNSET
        elif isinstance(self.attachments, list):
            attachments = self.attachments

        else:
            attachments = self.attachments

        auto_approve_plan = self.auto_approve_plan

        warning: None | str | Unset
        if isinstance(self.warning, Unset):
            warning = UNSET
        else:
            warning = self.warning

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created": created,
                "title": title,
                "project_id": project_id,
            }
        )
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
        if warning is not UNSET:
            field_dict["warning"] = warning

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        created = d.pop("created")

        title = d.pop("title")

        project_id = d.pop("project_id")

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

        def _parse_attachments(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                attachments_type_0 = cast(list[str], data)

                return attachments_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        attachments = _parse_attachments(d.pop("attachments", UNSET))

        auto_approve_plan = d.pop("auto_approve_plan", UNSET)

        def _parse_warning(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        warning = _parse_warning(d.pop("warning", UNSET))

        create_task_response = cls(
            created=created,
            title=title,
            project_id=project_id,
            requires_approval=requires_approval,
            task_type=task_type,
            profile_id=profile_id,
            preferred_workspace_id=preferred_workspace_id,
            attachments=attachments,
            auto_approve_plan=auto_approve_plan,
            warning=warning,
        )

        create_task_response.additional_properties = d
        return create_task_response

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
