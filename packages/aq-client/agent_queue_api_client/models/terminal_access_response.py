from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TerminalAccessResponse")


@_attrs_define
class TerminalAccessResponse:
    """
    Attributes:
        status (str):
        code (int | Unset):  Default: 0.
        message (str | Unset):  Default: ''.
        retryable (bool | Unset):  Default: False.
    """

    status: str
    code: int | Unset = 0
    message: str | Unset = ""
    retryable: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        status = self.status

        code = self.code

        message = self.message

        retryable = self.retryable

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "status": status,
            }
        )
        if code is not UNSET:
            field_dict["code"] = code
        if message is not UNSET:
            field_dict["message"] = message
        if retryable is not UNSET:
            field_dict["retryable"] = retryable

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        status = d.pop("status")

        code = d.pop("code", UNSET)

        message = d.pop("message", UNSET)

        retryable = d.pop("retryable", UNSET)

        terminal_access_response = cls(
            status=status,
            code=code,
            message=message,
            retryable=retryable,
        )

        terminal_access_response.additional_properties = d
        return terminal_access_response

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
