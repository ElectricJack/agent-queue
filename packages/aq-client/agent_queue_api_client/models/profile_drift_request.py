from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProfileDriftRequest")


@_attrs_define
class ProfileDriftRequest:
    """
    Attributes:
        profile_id (None | str | Unset): Only report this system profile
        drifted_only (bool | None | Unset): Only report profiles that diverge
    """

    profile_id: None | str | Unset = UNSET
    drifted_only: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        drifted_only: bool | None | Unset
        if isinstance(self.drifted_only, Unset):
            drifted_only = UNSET
        else:
            drifted_only = self.drifted_only

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if drifted_only is not UNSET:
            field_dict["drifted_only"] = drifted_only

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_drifted_only(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        drifted_only = _parse_drifted_only(d.pop("drifted_only", UNSET))

        profile_drift_request = cls(
            profile_id=profile_id,
            drifted_only=drifted_only,
        )

        profile_drift_request.additional_properties = d
        return profile_drift_request

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
