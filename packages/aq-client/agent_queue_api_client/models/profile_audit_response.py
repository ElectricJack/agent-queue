from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.profile_audit_row import ProfileAuditRow


T = TypeVar("T", bound="ProfileAuditResponse")


@_attrs_define
class ProfileAuditResponse:
    """
    Attributes:
        profiles (list[ProfileAuditRow] | Unset):
        legacy_count (int | Unset):  Default: 0.
        enforcement (str | Unset):  Default: 'audit'.
    """

    profiles: list[ProfileAuditRow] | Unset = UNSET
    legacy_count: int | Unset = 0
    enforcement: str | Unset = "audit"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profiles: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.profiles, Unset):
            profiles = []
            for profiles_item_data in self.profiles:
                profiles_item = profiles_item_data.to_dict()
                profiles.append(profiles_item)

        legacy_count = self.legacy_count

        enforcement = self.enforcement

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if profiles is not UNSET:
            field_dict["profiles"] = profiles
        if legacy_count is not UNSET:
            field_dict["legacy_count"] = legacy_count
        if enforcement is not UNSET:
            field_dict["enforcement"] = enforcement

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.profile_audit_row import ProfileAuditRow  # noqa: PLC0415

        d = dict(src_dict)
        _profiles = d.pop("profiles", UNSET)
        profiles: list[ProfileAuditRow] | Unset = UNSET
        if _profiles is not UNSET:
            profiles = []
            for profiles_item_data in _profiles:
                profiles_item = ProfileAuditRow.from_dict(profiles_item_data)

                profiles.append(profiles_item)

        legacy_count = d.pop("legacy_count", UNSET)

        enforcement = d.pop("enforcement", UNSET)

        profile_audit_response = cls(
            profiles=profiles,
            legacy_count=legacy_count,
            enforcement=enforcement,
        )

        profile_audit_response.additional_properties = d
        return profile_audit_response

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
