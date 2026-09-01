from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.get_mcp_server_response_adapter_config import GetMcpServerResponseAdapterConfig
    from ..models.get_mcp_server_response_env import GetMcpServerResponseEnv
    from ..models.get_mcp_server_response_headers import GetMcpServerResponseHeaders


T = TypeVar("T", bound="GetMcpServerResponse")


@_attrs_define
class GetMcpServerResponse:
    """
    Attributes:
        name (str):
        transport (str):
        scope (str):
        description (str | Unset):  Default: ''.
        is_builtin (bool | Unset):  Default: False.
        project_id (None | str | Unset):
        tool_count (int | None | Unset):
        last_probed_at (float | None | Unset):
        last_error (None | str | Unset):
        command (str | Unset):  Default: ''.
        args (list[str] | Unset):
        env (GetMcpServerResponseEnv | Unset):
        url (str | Unset):  Default: ''.
        headers (GetMcpServerResponseHeaders | Unset):
        notes (str | Unset):  Default: ''.
        adapter_config (GetMcpServerResponseAdapterConfig | Unset):
    """

    name: str
    transport: str
    scope: str
    description: str | Unset = ""
    is_builtin: bool | Unset = False
    project_id: None | str | Unset = UNSET
    tool_count: int | None | Unset = UNSET
    last_probed_at: float | None | Unset = UNSET
    last_error: None | str | Unset = UNSET
    command: str | Unset = ""
    args: list[str] | Unset = UNSET
    env: GetMcpServerResponseEnv | Unset = UNSET
    url: str | Unset = ""
    headers: GetMcpServerResponseHeaders | Unset = UNSET
    notes: str | Unset = ""
    adapter_config: GetMcpServerResponseAdapterConfig | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        transport = self.transport

        scope = self.scope

        description = self.description

        is_builtin = self.is_builtin

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        tool_count: int | None | Unset
        if isinstance(self.tool_count, Unset):
            tool_count = UNSET
        else:
            tool_count = self.tool_count

        last_probed_at: float | None | Unset
        if isinstance(self.last_probed_at, Unset):
            last_probed_at = UNSET
        else:
            last_probed_at = self.last_probed_at

        last_error: None | str | Unset
        if isinstance(self.last_error, Unset):
            last_error = UNSET
        else:
            last_error = self.last_error

        command = self.command

        args: list[str] | Unset = UNSET
        if not isinstance(self.args, Unset):
            args = self.args

        env: dict[str, Any] | Unset = UNSET
        if not isinstance(self.env, Unset):
            env = self.env.to_dict()

        url = self.url

        headers: dict[str, Any] | Unset = UNSET
        if not isinstance(self.headers, Unset):
            headers = self.headers.to_dict()

        notes = self.notes

        adapter_config: dict[str, Any] | Unset = UNSET
        if not isinstance(self.adapter_config, Unset):
            adapter_config = self.adapter_config.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "transport": transport,
                "scope": scope,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if is_builtin is not UNSET:
            field_dict["is_builtin"] = is_builtin
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if tool_count is not UNSET:
            field_dict["tool_count"] = tool_count
        if last_probed_at is not UNSET:
            field_dict["last_probed_at"] = last_probed_at
        if last_error is not UNSET:
            field_dict["last_error"] = last_error
        if command is not UNSET:
            field_dict["command"] = command
        if args is not UNSET:
            field_dict["args"] = args
        if env is not UNSET:
            field_dict["env"] = env
        if url is not UNSET:
            field_dict["url"] = url
        if headers is not UNSET:
            field_dict["headers"] = headers
        if notes is not UNSET:
            field_dict["notes"] = notes
        if adapter_config is not UNSET:
            field_dict["adapter_config"] = adapter_config

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.get_mcp_server_response_adapter_config import GetMcpServerResponseAdapterConfig
        from ..models.get_mcp_server_response_env import GetMcpServerResponseEnv
        from ..models.get_mcp_server_response_headers import GetMcpServerResponseHeaders

        d = dict(src_dict)
        name = d.pop("name")

        transport = d.pop("transport")

        scope = d.pop("scope")

        description = d.pop("description", UNSET)

        is_builtin = d.pop("is_builtin", UNSET)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_tool_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        tool_count = _parse_tool_count(d.pop("tool_count", UNSET))

        def _parse_last_probed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        last_probed_at = _parse_last_probed_at(d.pop("last_probed_at", UNSET))

        def _parse_last_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        last_error = _parse_last_error(d.pop("last_error", UNSET))

        command = d.pop("command", UNSET)

        args = cast(list[str], d.pop("args", UNSET))

        _env = d.pop("env", UNSET)
        env: GetMcpServerResponseEnv | Unset
        if isinstance(_env, Unset):
            env = UNSET
        else:
            env = GetMcpServerResponseEnv.from_dict(_env)

        url = d.pop("url", UNSET)

        _headers = d.pop("headers", UNSET)
        headers: GetMcpServerResponseHeaders | Unset
        if isinstance(_headers, Unset):
            headers = UNSET
        else:
            headers = GetMcpServerResponseHeaders.from_dict(_headers)

        notes = d.pop("notes", UNSET)

        _adapter_config = d.pop("adapter_config", UNSET)
        adapter_config: GetMcpServerResponseAdapterConfig | Unset
        if isinstance(_adapter_config, Unset):
            adapter_config = UNSET
        else:
            adapter_config = GetMcpServerResponseAdapterConfig.from_dict(_adapter_config)

        get_mcp_server_response = cls(
            name=name,
            transport=transport,
            scope=scope,
            description=description,
            is_builtin=is_builtin,
            project_id=project_id,
            tool_count=tool_count,
            last_probed_at=last_probed_at,
            last_error=last_error,
            command=command,
            args=args,
            env=env,
            url=url,
            headers=headers,
            notes=notes,
            adapter_config=adapter_config,
        )

        get_mcp_server_response.additional_properties = d
        return get_mcp_server_response

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
