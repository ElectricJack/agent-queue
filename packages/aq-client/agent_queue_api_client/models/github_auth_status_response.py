from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.github_auth_status_response_credential_mode import GithubAuthStatusResponseCredentialMode
from ..types import UNSET, Unset

T = TypeVar("T", bound="GithubAuthStatusResponse")


@_attrs_define
class GithubAuthStatusResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        installed (bool | Unset):  Default: False.
        authenticated (bool | Unset):  Default: False.
        credential_mode (GithubAuthStatusResponseCredentialMode | Unset):  Default:
            GithubAuthStatusResponseCredentialMode.EXISTING_LOGIN.
        repository_access (bool | None | Unset):
        account_operations_available (bool | Unset):  Default: True.
        configuration_changes_require_restart (bool | Unset):  Default: True.
        app_id (int | None | Unset):
        installation_id (int | None | Unset):
        host (None | str | Unset):
        login (None | str | Unset):
        cli_version (None | str | Unset):
        message (None | str | Unset):
    """

    success: bool | Unset = True
    installed: bool | Unset = False
    authenticated: bool | Unset = False
    credential_mode: GithubAuthStatusResponseCredentialMode | Unset = (
        GithubAuthStatusResponseCredentialMode.EXISTING_LOGIN
    )
    repository_access: bool | None | Unset = UNSET
    account_operations_available: bool | Unset = True
    configuration_changes_require_restart: bool | Unset = True
    app_id: int | None | Unset = UNSET
    installation_id: int | None | Unset = UNSET
    host: None | str | Unset = UNSET
    login: None | str | Unset = UNSET
    cli_version: None | str | Unset = UNSET
    message: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        installed = self.installed

        authenticated = self.authenticated

        credential_mode: str | Unset = UNSET
        if not isinstance(self.credential_mode, Unset):
            credential_mode = self.credential_mode.value

        repository_access: bool | None | Unset
        if isinstance(self.repository_access, Unset):
            repository_access = UNSET
        else:
            repository_access = self.repository_access

        account_operations_available = self.account_operations_available

        configuration_changes_require_restart = self.configuration_changes_require_restart

        app_id: int | None | Unset
        if isinstance(self.app_id, Unset):
            app_id = UNSET
        else:
            app_id = self.app_id

        installation_id: int | None | Unset
        if isinstance(self.installation_id, Unset):
            installation_id = UNSET
        else:
            installation_id = self.installation_id

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
        if credential_mode is not UNSET:
            field_dict["credential_mode"] = credential_mode
        if repository_access is not UNSET:
            field_dict["repository_access"] = repository_access
        if account_operations_available is not UNSET:
            field_dict["account_operations_available"] = account_operations_available
        if configuration_changes_require_restart is not UNSET:
            field_dict["configuration_changes_require_restart"] = configuration_changes_require_restart
        if app_id is not UNSET:
            field_dict["app_id"] = app_id
        if installation_id is not UNSET:
            field_dict["installation_id"] = installation_id
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

        _credential_mode = d.pop("credential_mode", UNSET)
        credential_mode: GithubAuthStatusResponseCredentialMode | Unset
        if isinstance(_credential_mode, Unset):
            credential_mode = UNSET
        else:
            credential_mode = GithubAuthStatusResponseCredentialMode(_credential_mode)

        def _parse_repository_access(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        repository_access = _parse_repository_access(d.pop("repository_access", UNSET))

        account_operations_available = d.pop("account_operations_available", UNSET)

        configuration_changes_require_restart = d.pop("configuration_changes_require_restart", UNSET)

        def _parse_app_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        app_id = _parse_app_id(d.pop("app_id", UNSET))

        def _parse_installation_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        installation_id = _parse_installation_id(d.pop("installation_id", UNSET))

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
            credential_mode=credential_mode,
            repository_access=repository_access,
            account_operations_available=account_operations_available,
            configuration_changes_require_restart=configuration_changes_require_restart,
            app_id=app_id,
            installation_id=installation_id,
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
