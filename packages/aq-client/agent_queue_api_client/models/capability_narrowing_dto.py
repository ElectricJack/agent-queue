from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="CapabilityNarrowingDTO")


@_attrs_define
class CapabilityNarrowingDTO:
    """``AgentTaskStep.capability_narrowing`` projected.

    Unlike :class:`CapabilityNamespacesDTO` every namespace is nullable, because
    the narrowing's ``None`` (this step narrows nothing here) and ``[]`` (none)
    are different instructions and the card has to be able to say which one the
    author wrote.  Lists are sorted for a stable card.

        Attributes:
            harness_tools (list[str] | None | Unset):
            aq_commands (list[str] | None | Unset):
            plugin_tools (list[str] | None | Unset):
    """

    harness_tools: list[str] | None | Unset = UNSET
    aq_commands: list[str] | None | Unset = UNSET
    plugin_tools: list[str] | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        harness_tools: list[str] | None | Unset
        if isinstance(self.harness_tools, Unset):
            harness_tools = UNSET
        elif isinstance(self.harness_tools, list):
            harness_tools = self.harness_tools

        else:
            harness_tools = self.harness_tools

        aq_commands: list[str] | None | Unset
        if isinstance(self.aq_commands, Unset):
            aq_commands = UNSET
        elif isinstance(self.aq_commands, list):
            aq_commands = self.aq_commands

        else:
            aq_commands = self.aq_commands

        plugin_tools: list[str] | None | Unset
        if isinstance(self.plugin_tools, Unset):
            plugin_tools = UNSET
        elif isinstance(self.plugin_tools, list):
            plugin_tools = self.plugin_tools

        else:
            plugin_tools = self.plugin_tools

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if harness_tools is not UNSET:
            field_dict["harness_tools"] = harness_tools
        if aq_commands is not UNSET:
            field_dict["aq_commands"] = aq_commands
        if plugin_tools is not UNSET:
            field_dict["plugin_tools"] = plugin_tools

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_harness_tools(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                harness_tools_type_0 = cast(list[str], data)

                return harness_tools_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        harness_tools = _parse_harness_tools(d.pop("harness_tools", UNSET))

        def _parse_aq_commands(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                aq_commands_type_0 = cast(list[str], data)

                return aq_commands_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        aq_commands = _parse_aq_commands(d.pop("aq_commands", UNSET))

        def _parse_plugin_tools(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                plugin_tools_type_0 = cast(list[str], data)

                return plugin_tools_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        plugin_tools = _parse_plugin_tools(d.pop("plugin_tools", UNSET))

        capability_narrowing_dto = cls(
            harness_tools=harness_tools,
            aq_commands=aq_commands,
            plugin_tools=plugin_tools,
        )

        return capability_narrowing_dto
