from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.edit_mcp_server_request_env_type_0 import EditMcpServerRequestEnvType0
    from ..models.edit_mcp_server_request_headers_type_0 import EditMcpServerRequestHeadersType0


T = TypeVar("T", bound="EditMcpServerRequest")


@_attrs_define
class EditMcpServerRequest:
    """
    Attributes:
        name (str):
        project_id (None | str | Unset):
        transport (None | str | Unset):
        description (None | str | Unset):
        notes (None | str | Unset):
        command (None | str | Unset):
        args (list[Any] | None | Unset):
        env (EditMcpServerRequestEnvType0 | None | Unset):
        url (None | str | Unset):
        headers (EditMcpServerRequestHeadersType0 | None | Unset):
    """

    name: str
    project_id: None | str | Unset = UNSET
    transport: None | str | Unset = UNSET
    description: None | str | Unset = UNSET
    notes: None | str | Unset = UNSET
    command: None | str | Unset = UNSET
    args: list[Any] | None | Unset = UNSET
    env: EditMcpServerRequestEnvType0 | None | Unset = UNSET
    url: None | str | Unset = UNSET
    headers: EditMcpServerRequestHeadersType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.edit_mcp_server_request_env_type_0 import EditMcpServerRequestEnvType0  # noqa: PLC0415
        from ..models.edit_mcp_server_request_headers_type_0 import EditMcpServerRequestHeadersType0  # noqa: PLC0415

        name = self.name

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        transport: None | str | Unset
        if isinstance(self.transport, Unset):
            transport = UNSET
        else:
            transport = self.transport

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        notes: None | str | Unset
        if isinstance(self.notes, Unset):
            notes = UNSET
        else:
            notes = self.notes

        command: None | str | Unset
        if isinstance(self.command, Unset):
            command = UNSET
        else:
            command = self.command

        args: list[Any] | None | Unset
        if isinstance(self.args, Unset):
            args = UNSET
        elif isinstance(self.args, list):
            args = self.args

        else:
            args = self.args

        env: dict[str, Any] | None | Unset
        if isinstance(self.env, Unset):
            env = UNSET
        elif isinstance(self.env, EditMcpServerRequestEnvType0):
            env = self.env.to_dict()
        else:
            env = self.env

        url: None | str | Unset
        if isinstance(self.url, Unset):
            url = UNSET
        else:
            url = self.url

        headers: dict[str, Any] | None | Unset
        if isinstance(self.headers, Unset):
            headers = UNSET
        elif isinstance(self.headers, EditMcpServerRequestHeadersType0):
            headers = self.headers.to_dict()
        else:
            headers = self.headers

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if transport is not UNSET:
            field_dict["transport"] = transport
        if description is not UNSET:
            field_dict["description"] = description
        if notes is not UNSET:
            field_dict["notes"] = notes
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

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.edit_mcp_server_request_env_type_0 import EditMcpServerRequestEnvType0  # noqa: PLC0415
        from ..models.edit_mcp_server_request_headers_type_0 import EditMcpServerRequestHeadersType0  # noqa: PLC0415

        d = dict(src_dict)
        name = d.pop("name")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_transport(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        transport = _parse_transport(d.pop("transport", UNSET))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        def _parse_notes(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        notes = _parse_notes(d.pop("notes", UNSET))

        def _parse_command(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        command = _parse_command(d.pop("command", UNSET))

        def _parse_args(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                args_type_0 = cast(list[Any], data)

                return args_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        args = _parse_args(d.pop("args", UNSET))

        def _parse_env(data: object) -> EditMcpServerRequestEnvType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                env_type_0 = EditMcpServerRequestEnvType0.from_dict(data)

                return env_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EditMcpServerRequestEnvType0 | None | Unset, data)

        env = _parse_env(d.pop("env", UNSET))

        def _parse_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        url = _parse_url(d.pop("url", UNSET))

        def _parse_headers(data: object) -> EditMcpServerRequestHeadersType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                headers_type_0 = EditMcpServerRequestHeadersType0.from_dict(data)

                return headers_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EditMcpServerRequestHeadersType0 | None | Unset, data)

        headers = _parse_headers(d.pop("headers", UNSET))

        edit_mcp_server_request = cls(
            name=name,
            project_id=project_id,
            transport=transport,
            description=description,
            notes=notes,
            command=command,
            args=args,
            env=env,
            url=url,
            headers=headers,
        )

        edit_mcp_server_request.additional_properties = d
        return edit_mcp_server_request

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
