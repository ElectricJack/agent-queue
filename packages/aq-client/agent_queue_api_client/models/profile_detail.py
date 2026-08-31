from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.profile_detail_install import ProfileDetailInstall


T = TypeVar("T", bound="ProfileDetail")


@_attrs_define
class ProfileDetail:
    """
    Attributes:
        id (str):
        name (str):
        description (str | Unset):  Default: ''.
        model (str | Unset):  Default: ''.
        harness (None | str | Unset):
        default_class (str | Unset):  Default: ''.
        permission_mode (str | Unset):  Default: ''.
        allowed_tools (list[str] | Unset):
        mcp_servers (list[str] | Unset):
        system_prompt_suffix (str | Unset):  Default: ''.
        install (ProfileDetailInstall | Unset):
    """

    id: str
    name: str
    description: str | Unset = ""
    model: str | Unset = ""
    harness: None | str | Unset = UNSET
    default_class: str | Unset = ""
    permission_mode: str | Unset = ""
    allowed_tools: list[str] | Unset = UNSET
    mcp_servers: list[str] | Unset = UNSET
    system_prompt_suffix: str | Unset = ""
    install: ProfileDetailInstall | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        description = self.description

        model = self.model

        harness: None | str | Unset
        if isinstance(self.harness, Unset):
            harness = UNSET
        else:
            harness = self.harness

        default_class = self.default_class

        permission_mode = self.permission_mode

        allowed_tools: list[str] | Unset = UNSET
        if not isinstance(self.allowed_tools, Unset):
            allowed_tools = self.allowed_tools

        mcp_servers: list[str] | Unset = UNSET
        if not isinstance(self.mcp_servers, Unset):
            mcp_servers = self.mcp_servers

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
        if harness is not UNSET:
            field_dict["harness"] = harness
        if default_class is not UNSET:
            field_dict["default_class"] = default_class
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
        from ..models.profile_detail_install import ProfileDetailInstall

        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        description = d.pop("description", UNSET)

        model = d.pop("model", UNSET)

        def _parse_harness(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        harness = _parse_harness(d.pop("harness", UNSET))

        default_class = d.pop("default_class", UNSET)

        permission_mode = d.pop("permission_mode", UNSET)

        allowed_tools = cast(list[str], d.pop("allowed_tools", UNSET))

        mcp_servers = cast(list[str], d.pop("mcp_servers", UNSET))

        system_prompt_suffix = d.pop("system_prompt_suffix", UNSET)

        _install = d.pop("install", UNSET)
        install: ProfileDetailInstall | Unset
        if isinstance(_install, Unset):
            install = UNSET
        else:
            install = ProfileDetailInstall.from_dict(_install)

        profile_detail = cls(
            id=id,
            name=name,
            description=description,
            model=model,
            harness=harness,
            default_class=default_class,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            system_prompt_suffix=system_prompt_suffix,
            install=install,
        )

        profile_detail.additional_properties = d
        return profile_detail

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
