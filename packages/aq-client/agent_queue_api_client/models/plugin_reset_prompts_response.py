from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PluginResetPromptsResponse")


@_attrs_define
class PluginResetPromptsResponse:
    """
    Attributes:
        name (str):
        reset_count (int | Unset):  Default: 0.
        message (str | Unset):  Default: ''.
    """

    name: str
    reset_count: int | Unset = 0
    message: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        reset_count = self.reset_count

        message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
            }
        )
        if reset_count is not UNSET:
            field_dict["reset_count"] = reset_count
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        reset_count = d.pop("reset_count", UNSET)

        message = d.pop("message", UNSET)

        plugin_reset_prompts_response = cls(
            name=name,
            reset_count=reset_count,
            message=message,
        )

        plugin_reset_prompts_response.additional_properties = d
        return plugin_reset_prompts_response

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
