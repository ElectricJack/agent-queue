from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="CapabilityNamespacesDTO")


@_attrs_define
class CapabilityNamespacesDTO:
    """``CapabilityPolicy`` projected.  Sorted; empty list means deny-all.

    Attributes:
        harness_tools (list[str] | Unset):
        aq_commands (list[str] | Unset):
        plugin_tools (list[str] | Unset):
    """

    harness_tools: list[str] | Unset = UNSET
    aq_commands: list[str] | Unset = UNSET
    plugin_tools: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        harness_tools: list[str] | Unset = UNSET
        if not isinstance(self.harness_tools, Unset):
            harness_tools = self.harness_tools

        aq_commands: list[str] | Unset = UNSET
        if not isinstance(self.aq_commands, Unset):
            aq_commands = self.aq_commands

        plugin_tools: list[str] | Unset = UNSET
        if not isinstance(self.plugin_tools, Unset):
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
        harness_tools = cast(list[str], d.pop("harness_tools", UNSET))

        aq_commands = cast(list[str], d.pop("aq_commands", UNSET))

        plugin_tools = cast(list[str], d.pop("plugin_tools", UNSET))

        capability_namespaces_dto = cls(
            harness_tools=harness_tools,
            aq_commands=aq_commands,
            plugin_tools=plugin_tools,
        )

        return capability_namespaces_dto
