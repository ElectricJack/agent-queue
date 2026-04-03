from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CheckoutBranchResponse")


@_attrs_define
class CheckoutBranchResponse:
    """
    Attributes:
        project_id (str):
        branch (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        warning (None | str | Unset):
    """

    project_id: str
    branch: str | Unset = ""
    status: str | Unset = ""
    warning: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        branch = self.branch

        status = self.status

        warning: None | str | Unset
        if isinstance(self.warning, Unset):
            warning = UNSET
        else:
            warning = self.warning

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if branch is not UNSET:
            field_dict["branch"] = branch
        if status is not UNSET:
            field_dict["status"] = status
        if warning is not UNSET:
            field_dict["warning"] = warning

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        branch = d.pop("branch", UNSET)

        status = d.pop("status", UNSET)

        def _parse_warning(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        warning = _parse_warning(d.pop("warning", UNSET))

        checkout_branch_response = cls(
            project_id=project_id,
            branch=branch,
            status=status,
            warning=warning,
        )

        checkout_branch_response.additional_properties = d
        return checkout_branch_response

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
