from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.github_owner import GithubOwner


T = TypeVar("T", bound="ListGithubOwnersResponse")


@_attrs_define
class ListGithubOwnersResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        owners (list[GithubOwner] | Unset):
    """

    success: bool | Unset = True
    owners: list[GithubOwner] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        owners: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.owners, Unset):
            owners = []
            for owners_item_data in self.owners:
                owners_item = owners_item_data.to_dict()
                owners.append(owners_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if owners is not UNSET:
            field_dict["owners"] = owners

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.github_owner import GithubOwner

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _owners = d.pop("owners", UNSET)
        owners: list[GithubOwner] | Unset = UNSET
        if _owners is not UNSET:
            owners = []
            for owners_item_data in _owners:
                owners_item = GithubOwner.from_dict(owners_item_data)

                owners.append(owners_item)

        list_github_owners_response = cls(
            success=success,
            owners=owners,
        )

        list_github_owners_response.additional_properties = d
        return list_github_owners_response

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
