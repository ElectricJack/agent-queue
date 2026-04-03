from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SetDefaultBranchResponse")


@_attrs_define
class SetDefaultBranchResponse:
    """
    Attributes:
        project_id (str):
        default_branch (str):
        previous_branch (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        branch_created (bool | None | Unset):
    """

    project_id: str
    default_branch: str
    previous_branch: str | Unset = ""
    status: str | Unset = ""
    branch_created: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        default_branch = self.default_branch

        previous_branch = self.previous_branch

        status = self.status

        branch_created: bool | None | Unset
        if isinstance(self.branch_created, Unset):
            branch_created = UNSET
        else:
            branch_created = self.branch_created

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "default_branch": default_branch,
            }
        )
        if previous_branch is not UNSET:
            field_dict["previous_branch"] = previous_branch
        if status is not UNSET:
            field_dict["status"] = status
        if branch_created is not UNSET:
            field_dict["branch_created"] = branch_created

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        default_branch = d.pop("default_branch")

        previous_branch = d.pop("previous_branch", UNSET)

        status = d.pop("status", UNSET)

        def _parse_branch_created(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        branch_created = _parse_branch_created(d.pop("branch_created", UNSET))

        set_default_branch_response = cls(
            project_id=project_id,
            default_branch=default_branch,
            previous_branch=previous_branch,
            status=status,
            branch_created=branch_created,
        )

        set_default_branch_response.additional_properties = d
        return set_default_branch_response

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
