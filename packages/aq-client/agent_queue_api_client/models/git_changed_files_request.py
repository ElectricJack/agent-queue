from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GitChangedFilesRequest")


@_attrs_define
class GitChangedFilesRequest:
    """
    Attributes:
        base_branch (None | str | Unset): Base revision to compare against (default: project default branch). Accepts a
            branch name or a revision expression such as 'HEAD~1', 'HEAD^' or 'main@{1}'.
        project_id (None | str | Unset): Project ID
        workspace (None | str | Unset): Workspace name or ID (optional)
    """

    base_branch: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    workspace: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        base_branch: None | str | Unset
        if isinstance(self.base_branch, Unset):
            base_branch = UNSET
        else:
            base_branch = self.base_branch

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        workspace: None | str | Unset
        if isinstance(self.workspace, Unset):
            workspace = UNSET
        else:
            workspace = self.workspace

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if base_branch is not UNSET:
            field_dict["base_branch"] = base_branch
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if workspace is not UNSET:
            field_dict["workspace"] = workspace

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_base_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        base_branch = _parse_base_branch(d.pop("base_branch", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_workspace(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        workspace = _parse_workspace(d.pop("workspace", UNSET))

        git_changed_files_request = cls(
            base_branch=base_branch,
            project_id=project_id,
            workspace=workspace,
        )

        git_changed_files_request.additional_properties = d
        return git_changed_files_request

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
