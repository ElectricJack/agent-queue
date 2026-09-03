from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PoolSetLifecycleResponse")


@_attrs_define
class PoolSetLifecycleResponse:
    """
    Attributes:
        success (bool):
        profile_id (None | str | Unset):
        lifecycle (None | str | Unset):
        warnings (list[str] | Unset):
        error (None | str | Unset):
    """

    success: bool
    profile_id: None | str | Unset = UNSET
    lifecycle: None | str | Unset = UNSET
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

        lifecycle: None | str | Unset
        if isinstance(self.lifecycle, Unset):
            lifecycle = UNSET
        else:
            lifecycle = self.lifecycle

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
        if lifecycle is not UNSET:
            field_dict["lifecycle"] = lifecycle
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

        def _parse_lifecycle(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        lifecycle = _parse_lifecycle(d.pop("lifecycle", UNSET))

        warnings = cast(list[str], d.pop("warnings", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        pool_set_lifecycle_response = cls(
            success=success,
            profile_id=profile_id,
            lifecycle=lifecycle,
            warnings=warnings,
            error=error,
        )

        pool_set_lifecycle_response.additional_properties = d
        return pool_set_lifecycle_response

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
