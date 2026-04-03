from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="InstallProfileResponse")


@_attrs_define
class InstallProfileResponse:
    """
    Attributes:
        profile_id (str):
        installed (list[str] | Unset):
        already_present (list[str] | Unset):
        manual (list[str] | Unset):
        ready (bool | Unset):  Default: False.
    """

    profile_id: str
    installed: list[str] | Unset = UNSET
    already_present: list[str] | Unset = UNSET
    manual: list[str] | Unset = UNSET
    ready: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile_id = self.profile_id

        installed: list[str] | Unset = UNSET
        if not isinstance(self.installed, Unset):
            installed = self.installed

        already_present: list[str] | Unset = UNSET
        if not isinstance(self.already_present, Unset):
            already_present = self.already_present

        manual: list[str] | Unset = UNSET
        if not isinstance(self.manual, Unset):
            manual = self.manual

        ready = self.ready

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile_id": profile_id,
            }
        )
        if installed is not UNSET:
            field_dict["installed"] = installed
        if already_present is not UNSET:
            field_dict["already_present"] = already_present
        if manual is not UNSET:
            field_dict["manual"] = manual
        if ready is not UNSET:
            field_dict["ready"] = ready

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        installed = cast(list[str], d.pop("installed", UNSET))

        already_present = cast(list[str], d.pop("already_present", UNSET))

        manual = cast(list[str], d.pop("manual", UNSET))

        ready = d.pop("ready", UNSET)

        install_profile_response = cls(
            profile_id=profile_id,
            installed=installed,
            already_present=already_present,
            manual=manual,
            ready=ready,
        )

        install_profile_response.additional_properties = d
        return install_profile_response

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
