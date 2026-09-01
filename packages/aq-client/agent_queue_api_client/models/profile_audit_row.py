from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProfileAuditRow")


@_attrs_define
class ProfileAuditRow:
    """One profile's capability policy, as ``profile_audit`` reports it.

    Attributes:
        id (str):
        source (str | Unset):  Default: 'legacy'.
        harness_tools (list[str] | Unset):
        aq_commands (list[str] | Unset):
        plugin_tools (list[str] | Unset):
        fingerprint (None | str | Unset):
        error (None | str | Unset):
    """

    id: str
    source: str | Unset = "legacy"
    harness_tools: list[str] | Unset = UNSET
    aq_commands: list[str] | Unset = UNSET
    plugin_tools: list[str] | Unset = UNSET
    fingerprint: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        source = self.source

        harness_tools: list[str] | Unset = UNSET
        if not isinstance(self.harness_tools, Unset):
            harness_tools = self.harness_tools

        aq_commands: list[str] | Unset = UNSET
        if not isinstance(self.aq_commands, Unset):
            aq_commands = self.aq_commands

        plugin_tools: list[str] | Unset = UNSET
        if not isinstance(self.plugin_tools, Unset):
            plugin_tools = self.plugin_tools

        fingerprint: None | str | Unset
        if isinstance(self.fingerprint, Unset):
            fingerprint = UNSET
        else:
            fingerprint = self.fingerprint

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
            }
        )
        if source is not UNSET:
            field_dict["source"] = source
        if harness_tools is not UNSET:
            field_dict["harness_tools"] = harness_tools
        if aq_commands is not UNSET:
            field_dict["aq_commands"] = aq_commands
        if plugin_tools is not UNSET:
            field_dict["plugin_tools"] = plugin_tools
        if fingerprint is not UNSET:
            field_dict["fingerprint"] = fingerprint
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        source = d.pop("source", UNSET)

        harness_tools = cast(list[str], d.pop("harness_tools", UNSET))

        aq_commands = cast(list[str], d.pop("aq_commands", UNSET))

        plugin_tools = cast(list[str], d.pop("plugin_tools", UNSET))

        def _parse_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        fingerprint = _parse_fingerprint(d.pop("fingerprint", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        profile_audit_row = cls(
            id=id,
            source=source,
            harness_tools=harness_tools,
            aq_commands=aq_commands,
            plugin_tools=plugin_tools,
            fingerprint=fingerprint,
            error=error,
        )

        profile_audit_row.additional_properties = d
        return profile_audit_row

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
