from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProfileAuditRequest")


@_attrs_define
class ProfileAuditRequest:
    """
    Attributes:
        legacy_only (bool | None | Unset): Only report profiles that still need migration
    """

    legacy_only: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        legacy_only: bool | None | Unset
        if isinstance(self.legacy_only, Unset):
            legacy_only = UNSET
        else:
            legacy_only = self.legacy_only

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if legacy_only is not UNSET:
            field_dict["legacy_only"] = legacy_only

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_legacy_only(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        legacy_only = _parse_legacy_only(d.pop("legacy_only", UNSET))

        profile_audit_request = cls(
            legacy_only=legacy_only,
        )

        profile_audit_request.additional_properties = d
        return profile_audit_request

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
