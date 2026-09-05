from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GithubAuthStatusResponse")


@_attrs_define
class GithubAuthStatusResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        installed (bool | Unset):  Default: False.
        authenticated (bool | Unset):  Default: False.
        host (None | str | Unset):
        login (None | str | Unset):
        cli_version (None | str | Unset):
        message (None | str | Unset):
    """

    success: bool | Unset = True
    installed: bool | Unset = False
    authenticated: bool | Unset = False
    host: None | str | Unset = UNSET
    login: None | str | Unset = UNSET
    cli_version: None | str | Unset = UNSET
    message: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        installed = self.installed

        authenticated = self.authenticated

        host: None | str | Unset
        if isinstance(self.host, Unset):
            host = UNSET
        else:
            host = self.host

        login: None | str | Unset
        if isinstance(self.login, Unset):
            login = UNSET
        else:
            login = self.login

        cli_version: None | str | Unset
        if isinstance(self.cli_version, Unset):
            cli_version = UNSET
        else:
            cli_version = self.cli_version

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if installed is not UNSET:
            field_dict["installed"] = installed
        if authenticated is not UNSET:
            field_dict["authenticated"] = authenticated
        if host is not UNSET:
            field_dict["host"] = host
        if login is not UNSET:
            field_dict["login"] = login
        if cli_version is not UNSET:
            field_dict["cli_version"] = cli_version
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success", UNSET)

        installed = d.pop("installed", UNSET)

        authenticated = d.pop("authenticated", UNSET)

        def _parse_host(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        host = _parse_host(d.pop("host", UNSET))

        def _parse_login(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        login = _parse_login(d.pop("login", UNSET))

        def _parse_cli_version(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        cli_version = _parse_cli_version(d.pop("cli_version", UNSET))

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        github_auth_status_response = cls(
            success=success,
            installed=installed,
            authenticated=authenticated,
            host=host,
            login=login,
            cli_version=cli_version,
            message=message,
        )

        github_auth_status_response.additional_properties = d
        return github_auth_status_response

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
