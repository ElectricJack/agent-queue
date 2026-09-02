from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProfileConfigDivergence")


@_attrs_define
class ProfileConfigDivergence:
    """One ``## Config`` field whose vault value differs from the shipped one.

    Attributes:
        field (str):
        shipped (Any | Unset):
        vault (Any | Unset):
    """

    field: str
    shipped: Any | Unset = UNSET
    vault: Any | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        field = self.field

        shipped = self.shipped

        vault = self.vault

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "field": field,
            }
        )
        if shipped is not UNSET:
            field_dict["shipped"] = shipped
        if vault is not UNSET:
            field_dict["vault"] = vault

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        field = d.pop("field")

        shipped = d.pop("shipped", UNSET)

        vault = d.pop("vault", UNSET)

        profile_config_divergence = cls(
            field=field,
            shipped=shipped,
            vault=vault,
        )

        profile_config_divergence.additional_properties = d
        return profile_config_divergence

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
