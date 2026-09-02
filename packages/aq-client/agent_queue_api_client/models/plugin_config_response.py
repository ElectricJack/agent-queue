from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.plugin_config_response_config import PluginConfigResponseConfig


T = TypeVar("T", bound="PluginConfigResponse")


@_attrs_define
class PluginConfigResponse:
    """
    Attributes:
        name (str):
        config (PluginConfigResponseConfig | Unset):
        message (None | str | Unset):
    """

    name: str
    config: PluginConfigResponseConfig | Unset = UNSET
    message: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        config: dict[str, Any] | Unset = UNSET
        if not isinstance(self.config, Unset):
            config = self.config.to_dict()

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
            }
        )
        if config is not UNSET:
            field_dict["config"] = config
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.plugin_config_response_config import PluginConfigResponseConfig

        d = dict(src_dict)
        name = d.pop("name")

        _config = d.pop("config", UNSET)
        config: PluginConfigResponseConfig | Unset
        if isinstance(_config, Unset):
            config = UNSET
        else:
            config = PluginConfigResponseConfig.from_dict(_config)

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        plugin_config_response = cls(
            name=name,
            config=config,
            message=message,
        )

        plugin_config_response.additional_properties = d
        return plugin_config_response

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
