from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.profile_drift_row import ProfileDriftRow


T = TypeVar("T", bound="ProfileDriftResponse")


@_attrs_define
class ProfileDriftResponse:
    """
    Attributes:
        profiles (list[ProfileDriftRow] | Unset):
        checked (int | Unset):  Default: 0.
        drifted_count (int | Unset):  Default: 0.
    """

    profiles: list[ProfileDriftRow] | Unset = UNSET
    checked: int | Unset = 0
    drifted_count: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profiles: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.profiles, Unset):
            profiles = []
            for profiles_item_data in self.profiles:
                profiles_item = profiles_item_data.to_dict()
                profiles.append(profiles_item)

        checked = self.checked

        drifted_count = self.drifted_count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if profiles is not UNSET:
            field_dict["profiles"] = profiles
        if checked is not UNSET:
            field_dict["checked"] = checked
        if drifted_count is not UNSET:
            field_dict["drifted_count"] = drifted_count

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.profile_drift_row import ProfileDriftRow

        d = dict(src_dict)
        _profiles = d.pop("profiles", UNSET)
        profiles: list[ProfileDriftRow] | Unset = UNSET
        if _profiles is not UNSET:
            profiles = []
            for profiles_item_data in _profiles:
                profiles_item = ProfileDriftRow.from_dict(profiles_item_data)

                profiles.append(profiles_item)

        checked = d.pop("checked", UNSET)

        drifted_count = d.pop("drifted_count", UNSET)

        profile_drift_response = cls(
            profiles=profiles,
            checked=checked,
            drifted_count=drifted_count,
        )

        profile_drift_response.additional_properties = d
        return profile_drift_response

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
