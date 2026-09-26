from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReportRequestResponse")


@_attrs_define
class ReportRequestResponse:
    """
    Attributes:
        request_id (str):
        state (str):
        message_id (str):
        deadline (float):
        success (bool | Unset):  Default: True.
    """

    request_id: str
    state: str
    message_id: str
    deadline: float
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        request_id = self.request_id

        state = self.state

        message_id = self.message_id

        deadline = self.deadline

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "request_id": request_id,
                "state": state,
                "message_id": message_id,
                "deadline": deadline,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_id = d.pop("request_id")

        state = d.pop("state")

        message_id = d.pop("message_id")

        deadline = d.pop("deadline")

        success = d.pop("success", UNSET)

        report_request_response = cls(
            request_id=request_id,
            state=state,
            message_id=message_id,
            deadline=deadline,
            success=success,
        )

        report_request_response.additional_properties = d
        return report_request_response

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
