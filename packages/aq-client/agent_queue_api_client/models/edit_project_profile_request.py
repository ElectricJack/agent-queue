from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.edit_project_profile_request_install_type_0 import EditProjectProfileRequestInstallType0


T = TypeVar("T", bound="EditProjectProfileRequest")


@_attrs_define
class EditProjectProfileRequest:
    """
    Attributes:
        project_id (str):
        agent_type (str):
        name (None | str | Unset):
        description (None | str | Unset):
        model (None | str | Unset):
        permission_mode (None | str | Unset):
        allowed_tools (list[Any] | None | Unset):
        mcp_servers (list[Any] | None | Unset):
        system_prompt_suffix (None | str | Unset):
        install (EditProjectProfileRequestInstallType0 | None | Unset):
    """

    project_id: str
    agent_type: str
    name: None | str | Unset = UNSET
    description: None | str | Unset = UNSET
    model: None | str | Unset = UNSET
    permission_mode: None | str | Unset = UNSET
    allowed_tools: list[Any] | None | Unset = UNSET
    mcp_servers: list[Any] | None | Unset = UNSET
    system_prompt_suffix: None | str | Unset = UNSET
    install: EditProjectProfileRequestInstallType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.edit_project_profile_request_install_type_0 import EditProjectProfileRequestInstallType0

        project_id = self.project_id

        agent_type = self.agent_type

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

        model: None | str | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        permission_mode: None | str | Unset
        if isinstance(self.permission_mode, Unset):
            permission_mode = UNSET
        else:
            permission_mode = self.permission_mode

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

        install: dict[str, Any] | None | Unset
        if isinstance(self.install, Unset):
            install = UNSET
        elif isinstance(self.install, EditProjectProfileRequestInstallType0):
            install = self.install.to_dict()
        else:
            install = self.install

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "agent_type": agent_type,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
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
        from ..models.edit_project_profile_request_install_type_0 import EditProjectProfileRequestInstallType0

        d = dict(src_dict)
        project_id = d.pop("project_id")

        agent_type = d.pop("agent_type")

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

        def _parse_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model = _parse_model(d.pop("model", UNSET))

        def _parse_permission_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        permission_mode = _parse_permission_mode(d.pop("permission_mode", UNSET))

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

        def _parse_install(data: object) -> EditProjectProfileRequestInstallType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                install_type_0 = EditProjectProfileRequestInstallType0.from_dict(data)

                return install_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EditProjectProfileRequestInstallType0 | None | Unset, data)

        install = _parse_install(d.pop("install", UNSET))

        edit_project_profile_request = cls(
            project_id=project_id,
            agent_type=agent_type,
            name=name,
            description=description,
            model=model,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            system_prompt_suffix=system_prompt_suffix,
            install=install,
        )

        edit_project_profile_request.additional_properties = d
        return edit_project_profile_request

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
