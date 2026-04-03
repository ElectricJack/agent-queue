from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RegenerateProfileResponse")


@_attrs_define
class RegenerateProfileResponse:
    """
    Attributes:
        project_id (str):
        status (str | Unset):  Default: ''.
        profile (None | str | Unset):
        message (None | str | Unset):
    """

    project_id: str
    status: str | Unset = ""
    profile: None | str | Unset = UNSET
    message: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        status = self.status

        profile: None | str | Unset
        if isinstance(self.profile, Unset):
            profile = UNSET
        else:
            profile = self.profile

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if profile is not UNSET:
            field_dict["profile"] = profile
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        status = d.pop("status", UNSET)

        def _parse_profile(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile = _parse_profile(d.pop("profile", UNSET))

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        regenerate_profile_response = cls(
            project_id=project_id,
            status=status,
            profile=profile,
            message=message,
        )

        regenerate_profile_response.additional_properties = d
        return regenerate_profile_response

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
