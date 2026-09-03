from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateProfileRequest")


@_attrs_define
class CreateProfileRequest:
    """
    Attributes:
        id (str): Profile slug ID (e.g. 'reviewer', 'web-developer')
        name (str): Human-readable display name
        description (None | str | Unset): What this profile is for (optional)
        default_class (None | str | Unset): Default intelligence class id (optional)
        harness (None | str | Unset): CLI harness id (for example 'codex' or 'claude')
        permission_mode (None | str | Unset): Permission mode override (optional)
        codex_full_auto (bool | None | Unset): Enable Codex --full-auto (requires harness 'codex')
        claude_dangerously_skip_permissions (bool | None | Unset): Let Claude skip permission prompts (requires harness
            'claude')
        allowed_tools (list[Any] | None | Unset): Tool whitelist (e.g. ['Read', 'Glob', 'Grep', 'Bash'])
        mcp_servers (list[Any] | None | Unset): MCP server names from the registry (e.g. ['playwright']). A legacy name
            -> config mapping is reduced to its keys.
        system_prompt_suffix (None | str | Unset): Text appended to the agent's system prompt (optional)
    """

    id: str
    name: str
    description: None | str | Unset = UNSET
    default_class: None | str | Unset = UNSET
    harness: None | str | Unset = UNSET
    permission_mode: None | str | Unset = UNSET
    codex_full_auto: bool | None | Unset = UNSET
    claude_dangerously_skip_permissions: bool | None | Unset = UNSET
    allowed_tools: list[Any] | None | Unset = UNSET
    mcp_servers: list[Any] | None | Unset = UNSET
    system_prompt_suffix: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        default_class: None | str | Unset
        if isinstance(self.default_class, Unset):
            default_class = UNSET
        else:
            default_class = self.default_class

        harness: None | str | Unset
        if isinstance(self.harness, Unset):
            harness = UNSET
        else:
            harness = self.harness

        permission_mode: None | str | Unset
        if isinstance(self.permission_mode, Unset):
            permission_mode = UNSET
        else:
            permission_mode = self.permission_mode

        codex_full_auto: bool | None | Unset
        if isinstance(self.codex_full_auto, Unset):
            codex_full_auto = UNSET
        else:
            codex_full_auto = self.codex_full_auto

        claude_dangerously_skip_permissions: bool | None | Unset
        if isinstance(self.claude_dangerously_skip_permissions, Unset):
            claude_dangerously_skip_permissions = UNSET
        else:
            claude_dangerously_skip_permissions = self.claude_dangerously_skip_permissions

        allowed_tools: list[Any] | None | Unset
        if isinstance(self.allowed_tools, Unset):
            allowed_tools = UNSET
        elif isinstance(self.allowed_tools, list):
            allowed_tools = self.allowed_tools

        else:
            allowed_tools = self.allowed_tools

        mcp_servers: list[Any] | None | Unset
        if isinstance(self.mcp_servers, Unset):
            mcp_servers = UNSET
        elif isinstance(self.mcp_servers, list):
            mcp_servers = self.mcp_servers

        else:
            mcp_servers = self.mcp_servers

        system_prompt_suffix: None | str | Unset
        if isinstance(self.system_prompt_suffix, Unset):
            system_prompt_suffix = UNSET
        else:
            system_prompt_suffix = self.system_prompt_suffix

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
        if default_class is not UNSET:
            field_dict["default_class"] = default_class
        if harness is not UNSET:
            field_dict["harness"] = harness
        if permission_mode is not UNSET:
            field_dict["permission_mode"] = permission_mode
        if codex_full_auto is not UNSET:
            field_dict["codex_full_auto"] = codex_full_auto
        if claude_dangerously_skip_permissions is not UNSET:
            field_dict["claude_dangerously_skip_permissions"] = claude_dangerously_skip_permissions
        if allowed_tools is not UNSET:
            field_dict["allowed_tools"] = allowed_tools
        if mcp_servers is not UNSET:
            field_dict["mcp_servers"] = mcp_servers
        if system_prompt_suffix is not UNSET:
            field_dict["system_prompt_suffix"] = system_prompt_suffix

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        def _parse_default_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        default_class = _parse_default_class(d.pop("default_class", UNSET))

        def _parse_harness(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        harness = _parse_harness(d.pop("harness", UNSET))

        def _parse_permission_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        permission_mode = _parse_permission_mode(d.pop("permission_mode", UNSET))

        def _parse_codex_full_auto(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        codex_full_auto = _parse_codex_full_auto(d.pop("codex_full_auto", UNSET))

        def _parse_claude_dangerously_skip_permissions(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        claude_dangerously_skip_permissions = _parse_claude_dangerously_skip_permissions(
            d.pop("claude_dangerously_skip_permissions", UNSET)
        )

        def _parse_allowed_tools(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                allowed_tools_type_0 = cast(list[Any], data)

                return allowed_tools_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        allowed_tools = _parse_allowed_tools(d.pop("allowed_tools", UNSET))

        def _parse_mcp_servers(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                mcp_servers_type_0 = cast(list[Any], data)

                return mcp_servers_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        mcp_servers = _parse_mcp_servers(d.pop("mcp_servers", UNSET))

        def _parse_system_prompt_suffix(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        system_prompt_suffix = _parse_system_prompt_suffix(d.pop("system_prompt_suffix", UNSET))

        create_profile_request = cls(
            id=id,
            name=name,
            description=description,
            default_class=default_class,
            harness=harness,
            permission_mode=permission_mode,
            codex_full_auto=codex_full_auto,
            claude_dangerously_skip_permissions=claude_dangerously_skip_permissions,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            system_prompt_suffix=system_prompt_suffix,
        )

        create_profile_request.additional_properties = d
        return create_profile_request

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
