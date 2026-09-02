from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProfileReseedResponse")


@_attrs_define
class ProfileReseedResponse:
    """
    Attributes:
        profile_id (str):
        path (str | Unset):  Default: ''.
        backup_path (None | str | Unset):
        created (bool | Unset):  Default: False.
        unretired (bool | Unset):  Default: False.
        warnings (list[str] | None | Unset):
        sync_errors (list[str] | None | Unset):
    """

    profile_id: str
    path: str | Unset = ""
    backup_path: None | str | Unset = UNSET
    created: bool | Unset = False
    unretired: bool | Unset = False
    warnings: list[str] | None | Unset = UNSET
    sync_errors: list[str] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile_id = self.profile_id

        path = self.path

        backup_path: None | str | Unset
        if isinstance(self.backup_path, Unset):
            backup_path = UNSET
        else:
            backup_path = self.backup_path

        created = self.created

        unretired = self.unretired

        warnings: list[str] | None | Unset
        if isinstance(self.warnings, Unset):
            warnings = UNSET
        elif isinstance(self.warnings, list):
            warnings = self.warnings

        else:
            warnings = self.warnings

        sync_errors: list[str] | None | Unset
        if isinstance(self.sync_errors, Unset):
            sync_errors = UNSET
        elif isinstance(self.sync_errors, list):
            sync_errors = self.sync_errors

        else:
            sync_errors = self.sync_errors

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile_id": profile_id,
            }
        )
        if path is not UNSET:
            field_dict["path"] = path
        if backup_path is not UNSET:
            field_dict["backup_path"] = backup_path
        if created is not UNSET:
            field_dict["created"] = created
        if unretired is not UNSET:
            field_dict["unretired"] = unretired
        if warnings is not UNSET:
            field_dict["warnings"] = warnings
        if sync_errors is not UNSET:
            field_dict["sync_errors"] = sync_errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        path = d.pop("path", UNSET)

        def _parse_backup_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        backup_path = _parse_backup_path(d.pop("backup_path", UNSET))

        created = d.pop("created", UNSET)

        unretired = d.pop("unretired", UNSET)

        def _parse_warnings(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                warnings_type_0 = cast(list[str], data)

                return warnings_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        warnings = _parse_warnings(d.pop("warnings", UNSET))

        def _parse_sync_errors(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                sync_errors_type_0 = cast(list[str], data)

                return sync_errors_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        sync_errors = _parse_sync_errors(d.pop("sync_errors", UNSET))

        profile_reseed_response = cls(
            profile_id=profile_id,
            path=path,
            backup_path=backup_path,
            created=created,
            unretired=unretired,
            warnings=warnings,
            sync_errors=sync_errors,
        )

        profile_reseed_response.additional_properties = d
        return profile_reseed_response

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
