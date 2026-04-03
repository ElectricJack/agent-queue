from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.plugin_info_response_plugin import PluginInfoResponsePlugin


T = TypeVar("T", bound="PluginInfoResponse")


@_attrs_define
class PluginInfoResponse:
    """
    Attributes:
        plugin (PluginInfoResponsePlugin | Unset):
    """

    plugin: PluginInfoResponsePlugin | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        plugin: dict[str, Any] | Unset = UNSET
        if not isinstance(self.plugin, Unset):
            plugin = self.plugin.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if plugin is not UNSET:
            field_dict["plugin"] = plugin

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.plugin_info_response_plugin import PluginInfoResponsePlugin

        d = dict(src_dict)
        _plugin = d.pop("plugin", UNSET)
        plugin: PluginInfoResponsePlugin | Unset
        if isinstance(_plugin, Unset):
            plugin = UNSET
        else:
            plugin = PluginInfoResponsePlugin.from_dict(_plugin)

        plugin_info_response = cls(
            plugin=plugin,
        )

        plugin_info_response.additional_properties = d
        return plugin_info_response

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
