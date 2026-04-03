from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PluginSummary")


@_attrs_define
class PluginSummary:
    """
    Attributes:
        name (str):
        version (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        source_url (str | Unset):  Default: ''.
        description (None | str | Unset):
        commands (list[Any] | None | Unset):
        tools (list[Any] | None | Unset):
    """

    name: str
    version: str | Unset = ""
    status: str | Unset = ""
    source_url: str | Unset = ""
    description: None | str | Unset = UNSET
    commands: list[Any] | None | Unset = UNSET
    tools: list[Any] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        version = self.version

        status = self.status

        source_url = self.source_url

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        commands: list[Any] | None | Unset
        if isinstance(self.commands, Unset):
            commands = UNSET
        elif isinstance(self.commands, list):
            commands = self.commands

        else:
            commands = self.commands

        tools: list[Any] | None | Unset
        if isinstance(self.tools, Unset):
            tools = UNSET
        elif isinstance(self.tools, list):
            tools = self.tools

        else:
            tools = self.tools

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
            }
        )
        if version is not UNSET:
            field_dict["version"] = version
        if status is not UNSET:
            field_dict["status"] = status
        if source_url is not UNSET:
            field_dict["source_url"] = source_url
        if description is not UNSET:
            field_dict["description"] = description
        if commands is not UNSET:
            field_dict["commands"] = commands
        if tools is not UNSET:
            field_dict["tools"] = tools

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        version = d.pop("version", UNSET)

        status = d.pop("status", UNSET)

        source_url = d.pop("source_url", UNSET)

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        def _parse_commands(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                commands_type_0 = cast(list[Any], data)

                return commands_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        commands = _parse_commands(d.pop("commands", UNSET))

        def _parse_tools(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                tools_type_0 = cast(list[Any], data)

                return tools_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        tools = _parse_tools(d.pop("tools", UNSET))

        plugin_summary = cls(
            name=name,
            version=version,
            status=status,
            source_url=source_url,
            description=description,
            commands=commands,
            tools=tools,
        )

        plugin_summary.additional_properties = d
        return plugin_summary

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
