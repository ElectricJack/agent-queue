from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GitCreatePrRequest")


@_attrs_define
class GitCreatePrRequest:
    """
    Attributes:
        title (str): PR title
        body (str | Unset): PR description body (optional) Default: ''.
        branch (None | str | Unset): Head branch (defaults to current branch)
        base (None | str | Unset): Base branch (defaults to repo's default branch)
        project_id (None | str | Unset): Project ID (optional — inferred from active project)
        workspace (None | str | Unset): Workspace ID or name to operate on (optional — defaults to first workspace)
    """

    title: str
    body: str | Unset = ""
    branch: None | str | Unset = UNSET
    base: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    workspace: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        title = self.title

        body = self.body

        branch: None | str | Unset
        if isinstance(self.branch, Unset):
            branch = UNSET
        else:
            branch = self.branch

        base: None | str | Unset
        if isinstance(self.base, Unset):
            base = UNSET
        else:
            base = self.base

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
        field_dict.update(
            {
                "title": title,
            }
        )
        if body is not UNSET:
            field_dict["body"] = body
        if branch is not UNSET:
            field_dict["branch"] = branch
        if base is not UNSET:
            field_dict["base"] = base
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if workspace is not UNSET:
            field_dict["workspace"] = workspace

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        title = d.pop("title")

        body = d.pop("body", UNSET)

        def _parse_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        branch = _parse_branch(d.pop("branch", UNSET))

        def _parse_base(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        base = _parse_base(d.pop("base", UNSET))

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

        git_create_pr_request = cls(
            title=title,
            body=body,
            branch=branch,
            base=base,
            project_id=project_id,
            workspace=workspace,
        )

        git_create_pr_request.additional_properties = d
        return git_create_pr_request

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
