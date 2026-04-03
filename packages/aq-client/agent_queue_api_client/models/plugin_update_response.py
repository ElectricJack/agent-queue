from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PluginUpdateResponse")


@_attrs_define
class PluginUpdateResponse:
    """
    Attributes:
        updated (str):
        rev (str | Unset):  Default: ''.
        message (str | Unset):  Default: ''.
    """

    updated: str
    rev: str | Unset = ""
    message: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        updated = self.updated

        rev = self.rev

        message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "updated": updated,
            }
        )
        if rev is not UNSET:
            field_dict["rev"] = rev
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        updated = d.pop("updated")

        rev = d.pop("rev", UNSET)

        message = d.pop("message", UNSET)

        plugin_update_response = cls(
            updated=updated,
            rev=rev,
            message=message,
        )

        plugin_update_response.additional_properties = d
        return plugin_update_response

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
