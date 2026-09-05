from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProfileSummary")


@_attrs_define
class ProfileSummary:
    """
    Attributes:
        id (str):
        name (str):
        description (str | Unset):  Default: ''.
        harness (None | str | Unset):
        default_class (str | Unset):  Default: ''.
        codex_full_auto (bool | Unset):  Default: False.
        claude_dangerously_skip_permissions (bool | Unset):  Default: False.
        allowed_tools (list[str] | Unset):
        mcp_servers (list[str] | Unset):
        has_system_prompt (bool | Unset):  Default: False.
        lifecycle (str | Unset):  Default: 'task'.
        min_active (int | None | Unset):
        max_active (int | None | Unset):
    """

    id: str
    name: str
    description: str | Unset = ""
    harness: None | str | Unset = UNSET
    default_class: str | Unset = ""
    codex_full_auto: bool | Unset = False
    claude_dangerously_skip_permissions: bool | Unset = False
    allowed_tools: list[str] | Unset = UNSET
    mcp_servers: list[str] | Unset = UNSET
    has_system_prompt: bool | Unset = False
    lifecycle: str | Unset = "task"
    min_active: int | None | Unset = UNSET
    max_active: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        description = self.description

        harness: None | str | Unset
        if isinstance(self.harness, Unset):
            harness = UNSET
        else:
            harness = self.harness

        default_class = self.default_class

        codex_full_auto = self.codex_full_auto

        claude_dangerously_skip_permissions = self.claude_dangerously_skip_permissions

        allowed_tools: list[str] | Unset = UNSET
        if not isinstance(self.allowed_tools, Unset):
            allowed_tools = self.allowed_tools

        mcp_servers: list[str] | Unset = UNSET
        if not isinstance(self.mcp_servers, Unset):
            mcp_servers = self.mcp_servers

        has_system_prompt = self.has_system_prompt

        lifecycle = self.lifecycle

        min_active: int | None | Unset
        if isinstance(self.min_active, Unset):
            min_active = UNSET
        else:
            min_active = self.min_active

        max_active: int | None | Unset
        if isinstance(self.max_active, Unset):
            max_active = UNSET
        else:
            max_active = self.max_active

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if harness is not UNSET:
            field_dict["harness"] = harness
        if default_class is not UNSET:
            field_dict["default_class"] = default_class
        if codex_full_auto is not UNSET:
            field_dict["codex_full_auto"] = codex_full_auto
        if claude_dangerously_skip_permissions is not UNSET:
            field_dict["claude_dangerously_skip_permissions"] = claude_dangerously_skip_permissions
        if allowed_tools is not UNSET:
            field_dict["allowed_tools"] = allowed_tools
        if mcp_servers is not UNSET:
            field_dict["mcp_servers"] = mcp_servers
        if has_system_prompt is not UNSET:
            field_dict["has_system_prompt"] = has_system_prompt
        if lifecycle is not UNSET:
            field_dict["lifecycle"] = lifecycle
        if min_active is not UNSET:
            field_dict["min_active"] = min_active
        if max_active is not UNSET:
            field_dict["max_active"] = max_active

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        description = d.pop("description", UNSET)

        def _parse_harness(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        harness = _parse_harness(d.pop("harness", UNSET))

        default_class = d.pop("default_class", UNSET)

        codex_full_auto = d.pop("codex_full_auto", UNSET)

        claude_dangerously_skip_permissions = d.pop("claude_dangerously_skip_permissions", UNSET)

        allowed_tools = cast(list[str], d.pop("allowed_tools", UNSET))

        mcp_servers = cast(list[str], d.pop("mcp_servers", UNSET))

        has_system_prompt = d.pop("has_system_prompt", UNSET)

        lifecycle = d.pop("lifecycle", UNSET)

        def _parse_min_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        min_active = _parse_min_active(d.pop("min_active", UNSET))

        def _parse_max_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_active = _parse_max_active(d.pop("max_active", UNSET))

        profile_summary = cls(
            id=id,
            name=name,
            description=description,
            harness=harness,
            default_class=default_class,
            codex_full_auto=codex_full_auto,
            claude_dangerously_skip_permissions=claude_dangerously_skip_permissions,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            has_system_prompt=has_system_prompt,
            lifecycle=lifecycle,
            min_active=min_active,
            max_active=max_active,
        )

        profile_summary.additional_properties = d
        return profile_summary

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
