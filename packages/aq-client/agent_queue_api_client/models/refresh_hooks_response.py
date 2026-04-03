from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RefreshHooksResponse")


@_attrs_define
class RefreshHooksResponse:
    """
    Attributes:
        success (bool | Unset):  Default: False.
        rules_scanned (int | Unset):  Default: 0.
        active_rules (int | Unset):  Default: 0.
        hooks_regenerated (int | Unset):  Default: 0.
        hooks_unchanged (int | Unset):  Default: 0.
        errors (int | Unset):  Default: 0.
    """

    success: bool | Unset = False
    rules_scanned: int | Unset = 0
    active_rules: int | Unset = 0
    hooks_regenerated: int | Unset = 0
    hooks_unchanged: int | Unset = 0
    errors: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        rules_scanned = self.rules_scanned

        active_rules = self.active_rules

        hooks_regenerated = self.hooks_regenerated

        hooks_unchanged = self.hooks_unchanged

        errors = self.errors

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if rules_scanned is not UNSET:
            field_dict["rules_scanned"] = rules_scanned
        if active_rules is not UNSET:
            field_dict["active_rules"] = active_rules
        if hooks_regenerated is not UNSET:
            field_dict["hooks_regenerated"] = hooks_regenerated
        if hooks_unchanged is not UNSET:
            field_dict["hooks_unchanged"] = hooks_unchanged
        if errors is not UNSET:
            field_dict["errors"] = errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success", UNSET)

        rules_scanned = d.pop("rules_scanned", UNSET)

        active_rules = d.pop("active_rules", UNSET)

        hooks_regenerated = d.pop("hooks_regenerated", UNSET)

        hooks_unchanged = d.pop("hooks_unchanged", UNSET)

        errors = d.pop("errors", UNSET)

        refresh_hooks_response = cls(
            success=success,
            rules_scanned=rules_scanned,
            active_rules=active_rules,
            hooks_regenerated=hooks_regenerated,
            hooks_unchanged=hooks_unchanged,
            errors=errors,
        )

        refresh_hooks_response.additional_properties = d
        return refresh_hooks_response

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
