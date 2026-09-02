from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProfileReseedRequest")


@_attrs_define
class ProfileReseedRequest:
    """
    Attributes:
        profile_id (str): System profile ID to reseed
        backup (bool | None | Unset): Keep a .bak-<epoch> copy of the replaced file (default true)
    """

    profile_id: str
    backup: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile_id = self.profile_id

        backup: bool | None | Unset
        if isinstance(self.backup, Unset):
            backup = UNSET
        else:
            backup = self.backup

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile_id": profile_id,
            }
        )
        if backup is not UNSET:
            field_dict["backup"] = backup

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        def _parse_backup(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        backup = _parse_backup(d.pop("backup", UNSET))

        profile_reseed_request = cls(
            profile_id=profile_id,
            backup=backup,
        )

        profile_reseed_request.additional_properties = d
        return profile_reseed_request

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
