from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PortableConfigResponse")


@_attrs_define
class PortableConfigResponse:
    """Preview/export/import receipt. Fields vary by operation.

    Attributes:
        error (None | str | Unset):
        applied (bool | Unset):  Default: False.
        dry_run (bool | None | Unset):
        validation_errors (list[str] | Unset):
    """

    error: None | str | Unset = UNSET
    applied: bool | Unset = False
    dry_run: bool | None | Unset = UNSET
    validation_errors: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        applied = self.applied

        dry_run: bool | None | Unset
        if isinstance(self.dry_run, Unset):
            dry_run = UNSET
        else:
            dry_run = self.dry_run

        validation_errors: list[str] | Unset = UNSET
        if not isinstance(self.validation_errors, Unset):
            validation_errors = self.validation_errors

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if error is not UNSET:
            field_dict["error"] = error
        if applied is not UNSET:
            field_dict["applied"] = applied
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if validation_errors is not UNSET:
            field_dict["validation_errors"] = validation_errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        applied = d.pop("applied", UNSET)

        def _parse_dry_run(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        dry_run = _parse_dry_run(d.pop("dry_run", UNSET))

        validation_errors = cast(list[str], d.pop("validation_errors", UNSET))

        portable_config_response = cls(
            error=error,
            applied=applied,
            dry_run=dry_run,
            validation_errors=validation_errors,
        )

        portable_config_response.additional_properties = d
        return portable_config_response

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
