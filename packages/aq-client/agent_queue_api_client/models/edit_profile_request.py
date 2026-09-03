from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.edit_profile_request_install_type_0 import EditProfileRequestInstallType0


T = TypeVar("T", bound="EditProfileRequest")


@_attrs_define
class EditProfileRequest:
    """
    Attributes:
        profile_id (str): Profile ID to edit
        name (None | str | Unset): New display name (optional)
        description (None | str | Unset): New description (optional)
        harness (None | str | Unset): New CLI harness id (optional)
        permission_mode (None | str | Unset): New permission mode (optional)
        codex_full_auto (bool | None | Unset): Enable or disable Codex --full-auto
        claude_dangerously_skip_permissions (bool | None | Unset): Enable or disable Claude permission-prompt bypass
        allowed_tools (list[Any] | None | Unset): New tool whitelist (optional)
        mcp_servers (list[Any] | None | Unset): New MCP server names from the registry (optional). A legacy name ->
            config mapping is reduced to its keys.
        system_prompt_suffix (None | str | Unset): New system prompt suffix (optional)
        default_class (None | str | Unset): New default intelligence class id (optional)
        install (EditProfileRequestInstallType0 | None | Unset): New install manifest (optional)
    """

    profile_id: str
    name: None | str | Unset = UNSET
    description: None | str | Unset = UNSET
    harness: None | str | Unset = UNSET
    permission_mode: None | str | Unset = UNSET
    codex_full_auto: bool | None | Unset = UNSET
    claude_dangerously_skip_permissions: bool | None | Unset = UNSET
    allowed_tools: list[Any] | None | Unset = UNSET
    mcp_servers: list[Any] | None | Unset = UNSET
    system_prompt_suffix: None | str | Unset = UNSET
    default_class: None | str | Unset = UNSET
    install: EditProfileRequestInstallType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.edit_profile_request_install_type_0 import EditProfileRequestInstallType0

        profile_id = self.profile_id

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

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

        default_class: None | str | Unset
        if isinstance(self.default_class, Unset):
            default_class = UNSET
        else:
            default_class = self.default_class

        install: dict[str, Any] | None | Unset
        if isinstance(self.install, Unset):
            install = UNSET
        elif isinstance(self.install, EditProfileRequestInstallType0):
            install = self.install.to_dict()
        else:
            install = self.install

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile_id": profile_id,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if description is not UNSET:
            field_dict["description"] = description
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
        if default_class is not UNSET:
            field_dict["default_class"] = default_class
        if install is not UNSET:
            field_dict["install"] = install

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.edit_profile_request_install_type_0 import EditProfileRequestInstallType0

        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

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

        def _parse_default_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        default_class = _parse_default_class(d.pop("default_class", UNSET))

        def _parse_install(data: object) -> EditProfileRequestInstallType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                install_type_0 = EditProfileRequestInstallType0.from_dict(data)

                return install_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EditProfileRequestInstallType0 | None | Unset, data)

        install = _parse_install(d.pop("install", UNSET))

        edit_profile_request = cls(
            profile_id=profile_id,
            name=name,
            description=description,
            harness=harness,
            permission_mode=permission_mode,
            codex_full_auto=codex_full_auto,
            claude_dangerously_skip_permissions=claude_dangerously_skip_permissions,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            system_prompt_suffix=system_prompt_suffix,
            default_class=default_class,
            install=install,
        )

        edit_profile_request.additional_properties = d
        return edit_profile_request

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
