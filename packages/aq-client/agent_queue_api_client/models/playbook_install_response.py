from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_validation_error import PlaybookValidationError


T = TypeVar("T", bound="PlaybookInstallResponse")


@_attrs_define
class PlaybookInstallResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        errors (list[PlaybookValidationError] | Unset):
    """

    success: bool | Unset = True
    errors: list[PlaybookValidationError] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        errors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = []
            for errors_item_data in self.errors:
                errors_item = errors_item_data.to_dict()
                errors.append(errors_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if errors is not UNSET:
            field_dict["errors"] = errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_validation_error import PlaybookValidationError

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _errors = d.pop("errors", UNSET)
        errors: list[PlaybookValidationError] | Unset = UNSET
        if _errors is not UNSET:
            errors = []
            for errors_item_data in _errors:
                errors_item = PlaybookValidationError.from_dict(errors_item_data)

                errors.append(errors_item)

        playbook_install_response = cls(
            success=success,
            errors=errors,
        )

        playbook_install_response.additional_properties = d
        return playbook_install_response

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
