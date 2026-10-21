from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_error_response_fields import AskErrorResponseFields


T = TypeVar("T", bound="AskErrorResponse")


@_attrs_define
class AskErrorResponse:
    """
    Attributes:
        error (str):
        error_code (str):
        success (bool | Unset):  Default: False.
        fields (AskErrorResponseFields | Unset):
    """

    error: str
    error_code: str
    success: bool | Unset = False
    fields: AskErrorResponseFields | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        error = self.error

        error_code = self.error_code

        success = self.success

        fields: dict[str, Any] | Unset = UNSET
        if not isinstance(self.fields, Unset):
            fields = self.fields.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "error": error,
                "error_code": error_code,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if fields is not UNSET:
            field_dict["fields"] = fields

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_error_response_fields import AskErrorResponseFields

        d = dict(src_dict)
        error = d.pop("error")

        error_code = d.pop("error_code")

        success = d.pop("success", UNSET)

        _fields = d.pop("fields", UNSET)
        fields: AskErrorResponseFields | Unset
        if isinstance(_fields, Unset):
            fields = UNSET
        else:
            fields = AskErrorResponseFields.from_dict(_fields)

        ask_error_response = cls(
            error=error,
            error_code=error_code,
            success=success,
            fields=fields,
        )

        ask_error_response.additional_properties = d
        return ask_error_response

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
