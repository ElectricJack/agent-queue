from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PoolDeleteResponse")


@_attrs_define
class PoolDeleteResponse:
    """
    Attributes:
        success (bool):
        profile_id (None | str | Unset):
        deleted (None | str | Unset):
        retired (bool | Unset):  Default: False.
        depth (None | str | Unset):
        sessions (list[str] | Unset):
        warnings (list[str] | Unset):
        error (None | str | Unset):
    """

    success: bool
    profile_id: None | str | Unset = UNSET
    deleted: None | str | Unset = UNSET
    retired: bool | Unset = False
    depth: None | str | Unset = UNSET
    sessions: list[str] | Unset = UNSET
    warnings: list[str] | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        deleted: None | str | Unset
        if isinstance(self.deleted, Unset):
            deleted = UNSET
        else:
            deleted = self.deleted

        retired = self.retired

        depth: None | str | Unset
        if isinstance(self.depth, Unset):
            depth = UNSET
        else:
            depth = self.depth

        sessions: list[str] | Unset = UNSET
        if not isinstance(self.sessions, Unset):
            sessions = self.sessions

        warnings: list[str] | Unset = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = self.warnings

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
        if deleted is not UNSET:
            field_dict["deleted"] = deleted
        if retired is not UNSET:
            field_dict["retired"] = retired
        if depth is not UNSET:
            field_dict["depth"] = depth
        if sessions is not UNSET:
            field_dict["sessions"] = sessions
        if warnings is not UNSET:
            field_dict["warnings"] = warnings
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

        def _parse_deleted(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        deleted = _parse_deleted(d.pop("deleted", UNSET))

        retired = d.pop("retired", UNSET)

        def _parse_depth(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        depth = _parse_depth(d.pop("depth", UNSET))

        sessions = cast(list[str], d.pop("sessions", UNSET))

        warnings = cast(list[str], d.pop("warnings", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        pool_delete_response = cls(
            success=success,
            profile_id=profile_id,
            deleted=deleted,
            retired=retired,
            depth=depth,
            sessions=sessions,
            warnings=warnings,
            error=error,
        )

        pool_delete_response.additional_properties = d
        return pool_delete_response

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
