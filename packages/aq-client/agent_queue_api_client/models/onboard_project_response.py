from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.onboard_project_response_source_type import OnboardProjectResponseSourceType
from ..types import UNSET, Unset

T = TypeVar("T", bound="OnboardProjectResponse")


@_attrs_define
class OnboardProjectResponse:
    """The success payload of §5.3.

    Attributes:
        request_id (str):
        project_id (str):
        workspace_id (str):
        source_type (OnboardProjectResponseSourceType):
        root_id (str):
        relative_path (str):
        canonical_path (str):
        default_branch (str):
        success (bool | Unset):  Default: True.
        remote_url (None | str | Unset):
        actions (list[str] | Unset):
    """

    request_id: str
    project_id: str
    workspace_id: str
    source_type: OnboardProjectResponseSourceType
    root_id: str
    relative_path: str
    canonical_path: str
    default_branch: str
    success: bool | Unset = True
    remote_url: None | str | Unset = UNSET
    actions: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        request_id = self.request_id

        project_id = self.project_id

        workspace_id = self.workspace_id

        source_type = self.source_type.value

        root_id = self.root_id

        relative_path = self.relative_path

        canonical_path = self.canonical_path

        default_branch = self.default_branch

        success = self.success

        remote_url: None | str | Unset
        if isinstance(self.remote_url, Unset):
            remote_url = UNSET
        else:
            remote_url = self.remote_url

        actions: list[str] | Unset = UNSET
        if not isinstance(self.actions, Unset):
            actions = self.actions

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "request_id": request_id,
                "project_id": project_id,
                "workspace_id": workspace_id,
                "source_type": source_type,
                "root_id": root_id,
                "relative_path": relative_path,
                "canonical_path": canonical_path,
                "default_branch": default_branch,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if remote_url is not UNSET:
            field_dict["remote_url"] = remote_url
        if actions is not UNSET:
            field_dict["actions"] = actions

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_id = d.pop("request_id")

        project_id = d.pop("project_id")

        workspace_id = d.pop("workspace_id")

        source_type = OnboardProjectResponseSourceType(d.pop("source_type"))

        root_id = d.pop("root_id")

        relative_path = d.pop("relative_path")

        canonical_path = d.pop("canonical_path")

        default_branch = d.pop("default_branch")

        success = d.pop("success", UNSET)

        def _parse_remote_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        remote_url = _parse_remote_url(d.pop("remote_url", UNSET))

        actions = cast(list[str], d.pop("actions", UNSET))

        onboard_project_response = cls(
            request_id=request_id,
            project_id=project_id,
            workspace_id=workspace_id,
            source_type=source_type,
            root_id=root_id,
            relative_path=relative_path,
            canonical_path=canonical_path,
            default_branch=default_branch,
            success=success,
            remote_url=remote_url,
            actions=actions,
        )

        onboard_project_response.additional_properties = d
        return onboard_project_response

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
