from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DigestRequestResponse")


@_attrs_define
class DigestRequestResponse:
    """How many held windows this reconciliation handed to the supervisor.

    Attributes:
        success (bool | Unset):  Default: True.
        requested (int | Unset):  Default: 0.
        cancelled (int | Unset):  Default: 0.
    """

    success: bool | Unset = True
    requested: int | Unset = 0
    cancelled: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        requested = self.requested

        cancelled = self.cancelled

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if requested is not UNSET:
            field_dict["requested"] = requested
        if cancelled is not UNSET:
            field_dict["cancelled"] = cancelled

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success", UNSET)

        requested = d.pop("requested", UNSET)

        cancelled = d.pop("cancelled", UNSET)

        digest_request_response = cls(
            success=success,
            requested=requested,
            cancelled=cancelled,
        )

        digest_request_response.additional_properties = d
        return digest_request_response

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
