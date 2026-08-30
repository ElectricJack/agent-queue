from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SessionInputResponse")


@_attrs_define
class SessionInputResponse:
    """
    Attributes:
        session_id (str):
        success (bool | Unset):  Default: True.
        accepted (bool | Unset):  Default: True.
    """

    session_id: str
    success: bool | Unset = True
    accepted: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        session_id = self.session_id

        success = self.success

        accepted = self.accepted

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "session_id": session_id,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if accepted is not UNSET:
            field_dict["accepted"] = accepted

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        session_id = d.pop("session_id")

        success = d.pop("success", UNSET)

        accepted = d.pop("accepted", UNSET)

        session_input_response = cls(
            session_id=session_id,
            success=success,
            accepted=accepted,
        )

        session_input_response.additional_properties = d
        return session_input_response

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
