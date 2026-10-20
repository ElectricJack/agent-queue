from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ListWorkspacesRequest")


@_attrs_define
class ListWorkspacesRequest:
    """
    Attributes:
        project_id (None | str | Unset): Filter by project ID (optional)
        git (bool | None | Unset): Probe each checkout's branch and dirty state with git (default true). False returns
            lock and owner facts only.
        deadline_seconds (float | None | Unset): Overall time budget for diagnostics (default 10, clamped to 0.5-20).
            Unfinished probes report timeout/not_started.
    """

    project_id: None | str | Unset = UNSET
    git: bool | None | Unset = UNSET
    deadline_seconds: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        git: bool | None | Unset
        if isinstance(self.git, Unset):
            git = UNSET
        else:
            git = self.git

        deadline_seconds: float | None | Unset
        if isinstance(self.deadline_seconds, Unset):
            deadline_seconds = UNSET
        else:
            deadline_seconds = self.deadline_seconds

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if git is not UNSET:
            field_dict["git"] = git
        if deadline_seconds is not UNSET:
            field_dict["deadline_seconds"] = deadline_seconds

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

        def _parse_git(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        git = _parse_git(d.pop("git", UNSET))

        def _parse_deadline_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        deadline_seconds = _parse_deadline_seconds(d.pop("deadline_seconds", UNSET))

        list_workspaces_request = cls(
            project_id=project_id,
            git=git,
            deadline_seconds=deadline_seconds,
        )

        list_workspaces_request.additional_properties = d
        return list_workspaces_request

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
