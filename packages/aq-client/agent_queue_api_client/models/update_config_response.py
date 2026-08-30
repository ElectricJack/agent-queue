from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="UpdateConfigResponse")


@_attrs_define
class UpdateConfigResponse:
    """
    Attributes:
        applied (bool | Unset):  Default: False.
        changed (bool | Unset):  Default: False.
        requires_restart (bool | None | Unset):
        applied_sections (list[str] | Unset):
        validation_errors (list[str] | Unset):
        dry_run (bool | None | Unset):
        error (None | str | Unset):
    """

    applied: bool | Unset = False
    changed: bool | Unset = False
    requires_restart: bool | None | Unset = UNSET
    applied_sections: list[str] | Unset = UNSET
    validation_errors: list[str] | Unset = UNSET
    dry_run: bool | None | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        applied = self.applied

        changed = self.changed

        requires_restart: bool | None | Unset
        if isinstance(self.requires_restart, Unset):
            requires_restart = UNSET
        else:
            requires_restart = self.requires_restart

        applied_sections: list[str] | Unset = UNSET
        if not isinstance(self.applied_sections, Unset):
            applied_sections = self.applied_sections

        validation_errors: list[str] | Unset = UNSET
        if not isinstance(self.validation_errors, Unset):
            validation_errors = self.validation_errors

        dry_run: bool | None | Unset
        if isinstance(self.dry_run, Unset):
            dry_run = UNSET
        else:
            dry_run = self.dry_run

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if applied is not UNSET:
            field_dict["applied"] = applied
        if changed is not UNSET:
            field_dict["changed"] = changed
        if requires_restart is not UNSET:
            field_dict["requires_restart"] = requires_restart
        if applied_sections is not UNSET:
            field_dict["applied_sections"] = applied_sections
        if validation_errors is not UNSET:
            field_dict["validation_errors"] = validation_errors
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        applied = d.pop("applied", UNSET)

        changed = d.pop("changed", UNSET)

        def _parse_requires_restart(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        requires_restart = _parse_requires_restart(d.pop("requires_restart", UNSET))

        applied_sections = cast(list[str], d.pop("applied_sections", UNSET))

        validation_errors = cast(list[str], d.pop("validation_errors", UNSET))

        def _parse_dry_run(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        dry_run = _parse_dry_run(d.pop("dry_run", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        update_config_response = cls(
            applied=applied,
            changed=changed,
            requires_restart=requires_restart,
            applied_sections=applied_sections,
            validation_errors=validation_errors,
            dry_run=dry_run,
            error=error,
        )

        update_config_response.additional_properties = d
        return update_config_response

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
