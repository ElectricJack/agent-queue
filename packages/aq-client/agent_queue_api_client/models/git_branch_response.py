from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GitBranchResponse")


@_attrs_define
class GitBranchResponse:
    """
    Attributes:
        project_id (str):
        created (None | str | Unset):
        message (None | str | Unset):
        current_branch (None | str | Unset):
        branches (list[str] | None | Unset):
    """

    project_id: str
    created: None | str | Unset = UNSET
    message: None | str | Unset = UNSET
    current_branch: None | str | Unset = UNSET
    branches: list[str] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        created: None | str | Unset
        if isinstance(self.created, Unset):
            created = UNSET
        else:
            created = self.created

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        current_branch: None | str | Unset
        if isinstance(self.current_branch, Unset):
            current_branch = UNSET
        else:
            current_branch = self.current_branch

        branches: list[str] | None | Unset
        if isinstance(self.branches, Unset):
            branches = UNSET
        elif isinstance(self.branches, list):
            branches = self.branches

        else:
            branches = self.branches

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if created is not UNSET:
            field_dict["created"] = created
        if message is not UNSET:
            field_dict["message"] = message
        if current_branch is not UNSET:
            field_dict["current_branch"] = current_branch
        if branches is not UNSET:
            field_dict["branches"] = branches

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_created(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        created = _parse_created(d.pop("created", UNSET))

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        def _parse_current_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_branch = _parse_current_branch(d.pop("current_branch", UNSET))

        def _parse_branches(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                branches_type_0 = cast(list[str], data)

                return branches_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        branches = _parse_branches(d.pop("branches", UNSET))

        git_branch_response = cls(
            project_id=project_id,
            created=created,
            message=message,
            current_branch=current_branch,
            branches=branches,
        )

        git_branch_response.additional_properties = d
        return git_branch_response

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
