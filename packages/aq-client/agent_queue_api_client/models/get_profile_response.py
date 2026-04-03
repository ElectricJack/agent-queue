from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.get_profile_response_install import GetProfileResponseInstall
    from ..models.get_profile_response_mcp_servers import GetProfileResponseMcpServers


T = TypeVar("T", bound="GetProfileResponse")


@_attrs_define
class GetProfileResponse:
    """
    Attributes:
        id (str):
        name (str):
        description (str | Unset):  Default: ''.
        model (str | Unset):  Default: ''.
        permission_mode (str | Unset):  Default: ''.
        allowed_tools (list[str] | Unset):
        mcp_servers (GetProfileResponseMcpServers | Unset):
        system_prompt_suffix (str | Unset):  Default: ''.
        install (GetProfileResponseInstall | Unset):
    """

    id: str
    name: str
    description: str | Unset = ""
    model: str | Unset = ""
    permission_mode: str | Unset = ""
    allowed_tools: list[str] | Unset = UNSET
    mcp_servers: GetProfileResponseMcpServers | Unset = UNSET
    system_prompt_suffix: str | Unset = ""
    install: GetProfileResponseInstall | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        description = self.description

        model = self.model

        permission_mode = self.permission_mode

        allowed_tools: list[str] | Unset = UNSET
        if not isinstance(self.allowed_tools, Unset):
            allowed_tools = self.allowed_tools

        mcp_servers: dict[str, Any] | Unset = UNSET
        if not isinstance(self.mcp_servers, Unset):
            mcp_servers = self.mcp_servers.to_dict()

        system_prompt_suffix = self.system_prompt_suffix

        install: dict[str, Any] | Unset = UNSET
        if not isinstance(self.install, Unset):
            install = self.install.to_dict()

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
        if model is not UNSET:
            field_dict["model"] = model
        if permission_mode is not UNSET:
            field_dict["permission_mode"] = permission_mode
        if allowed_tools is not UNSET:
            field_dict["allowed_tools"] = allowed_tools
        if mcp_servers is not UNSET:
            field_dict["mcp_servers"] = mcp_servers
        if system_prompt_suffix is not UNSET:
            field_dict["system_prompt_suffix"] = system_prompt_suffix
        if install is not UNSET:
            field_dict["install"] = install

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.get_profile_response_install import GetProfileResponseInstall
        from ..models.get_profile_response_mcp_servers import GetProfileResponseMcpServers

        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        description = d.pop("description", UNSET)

        model = d.pop("model", UNSET)

        permission_mode = d.pop("permission_mode", UNSET)

        allowed_tools = cast(list[str], d.pop("allowed_tools", UNSET))

        _mcp_servers = d.pop("mcp_servers", UNSET)
        mcp_servers: GetProfileResponseMcpServers | Unset
        if isinstance(_mcp_servers, Unset):
            mcp_servers = UNSET
        else:
            mcp_servers = GetProfileResponseMcpServers.from_dict(_mcp_servers)

        system_prompt_suffix = d.pop("system_prompt_suffix", UNSET)

        _install = d.pop("install", UNSET)
        install: GetProfileResponseInstall | Unset
        if isinstance(_install, Unset):
            install = UNSET
        else:
            install = GetProfileResponseInstall.from_dict(_install)

        get_profile_response = cls(
            id=id,
            name=name,
            description=description,
            model=model,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            system_prompt_suffix=system_prompt_suffix,
            install=install,
        )

        get_profile_response.additional_properties = d
        return get_profile_response

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
