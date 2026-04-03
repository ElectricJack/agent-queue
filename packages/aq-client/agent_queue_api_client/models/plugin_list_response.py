from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.plugin_summary import PluginSummary


T = TypeVar("T", bound="PluginListResponse")


@_attrs_define
class PluginListResponse:
    """
    Attributes:
        plugins (list[PluginSummary] | Unset):
        count (int | Unset):  Default: 0.
    """

    plugins: list[PluginSummary] | Unset = UNSET
    count: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        plugins: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.plugins, Unset):
            plugins = []
            for plugins_item_data in self.plugins:
                plugins_item = plugins_item_data.to_dict()
                plugins.append(plugins_item)

        count = self.count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if plugins is not UNSET:
            field_dict["plugins"] = plugins
        if count is not UNSET:
            field_dict["count"] = count

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.plugin_summary import PluginSummary

        d = dict(src_dict)
        _plugins = d.pop("plugins", UNSET)
        plugins: list[PluginSummary] | Unset = UNSET
        if _plugins is not UNSET:
            plugins = []
            for plugins_item_data in _plugins:
                plugins_item = PluginSummary.from_dict(plugins_item_data)

                plugins.append(plugins_item)

        count = d.pop("count", UNSET)

        plugin_list_response = cls(
            plugins=plugins,
            count=count,
        )

        plugin_list_response.additional_properties = d
        return plugin_list_response

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
