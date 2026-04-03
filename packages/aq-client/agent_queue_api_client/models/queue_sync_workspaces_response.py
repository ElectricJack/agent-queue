from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="QueueSyncWorkspacesResponse")


@_attrs_define
class QueueSyncWorkspacesResponse:
    """
    Attributes:
        queued (str):
        project_id (str):
        title (str | Unset):  Default: ''.
        priority (int | Unset):  Default: 0.
        workspace_count (int | Unset):  Default: 0.
        default_branch (str | Unset):  Default: ''.
        message (str | Unset):  Default: ''.
    """

    queued: str
    project_id: str
    title: str | Unset = ""
    priority: int | Unset = 0
    workspace_count: int | Unset = 0
    default_branch: str | Unset = ""
    message: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        queued = self.queued

        project_id = self.project_id

        title = self.title

        priority = self.priority

        workspace_count = self.workspace_count

        default_branch = self.default_branch

        message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "queued": queued,
                "project_id": project_id,
            }
        )
        if title is not UNSET:
            field_dict["title"] = title
        if priority is not UNSET:
            field_dict["priority"] = priority
        if workspace_count is not UNSET:
            field_dict["workspace_count"] = workspace_count
        if default_branch is not UNSET:
            field_dict["default_branch"] = default_branch
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        queued = d.pop("queued")

        project_id = d.pop("project_id")

        title = d.pop("title", UNSET)

        priority = d.pop("priority", UNSET)

        workspace_count = d.pop("workspace_count", UNSET)

        default_branch = d.pop("default_branch", UNSET)

        message = d.pop("message", UNSET)

        queue_sync_workspaces_response = cls(
            queued=queued,
            project_id=project_id,
            title=title,
            priority=priority,
            workspace_count=workspace_count,
            default_branch=default_branch,
            message=message,
        )

        queue_sync_workspaces_response.additional_properties = d
        return queue_sync_workspaces_response

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
