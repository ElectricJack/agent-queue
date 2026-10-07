from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PoolRenameResponse")


@_attrs_define
class PoolRenameResponse:
    """
    Attributes:
        success (bool):
        profile_id (None | str | Unset):
        name (None | str | Unset):
        changed (bool | Unset):  Default: False.
        backup_path (None | str | Unset):
        error (None | str | Unset):
    """

    success: bool
    profile_id: None | str | Unset = UNSET
    name: None | str | Unset = UNSET
    changed: bool | Unset = False
    backup_path: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        changed = self.changed

        backup_path: None | str | Unset
        if isinstance(self.backup_path, Unset):
            backup_path = UNSET
        else:
            backup_path = self.backup_path

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
            }
        )
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if name is not UNSET:
            field_dict["name"] = name
        if changed is not UNSET:
            field_dict["changed"] = changed
        if backup_path is not UNSET:
            field_dict["backup_path"] = backup_path
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success")

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        changed = d.pop("changed", UNSET)

        def _parse_backup_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        backup_path = _parse_backup_path(d.pop("backup_path", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        pool_rename_response = cls(
            success=success,
            profile_id=profile_id,
            name=name,
            changed=changed,
            backup_path=backup_path,
            error=error,
        )

        pool_rename_response.additional_properties = d
        return pool_rename_response

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
