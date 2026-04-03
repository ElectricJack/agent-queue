from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ExportProfileRequest")


@_attrs_define
class ExportProfileRequest:
    """
    Attributes:
        profile_id (str): Profile ID to export
        create_gist (bool | None | Unset): If true, create a public GitHub gist and return the URL
    """

    profile_id: str
    create_gist: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile_id = self.profile_id

        create_gist: bool | None | Unset
        if isinstance(self.create_gist, Unset):
            create_gist = UNSET
        else:
            create_gist = self.create_gist

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile_id": profile_id,
            }
        )
        if create_gist is not UNSET:
            field_dict["create_gist"] = create_gist

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        def _parse_create_gist(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        create_gist = _parse_create_gist(d.pop("create_gist", UNSET))

        export_profile_request = cls(
            profile_id=profile_id,
            create_gist=create_gist,
        )

        export_profile_request.additional_properties = d
        return export_profile_request

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
