from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReviewFeedbackCompleteResponse")


@_attrs_define
class ReviewFeedbackCompleteResponse:
    """
    Attributes:
        obligation_id (str):
        recorded (bool):
        success (bool | Unset):  Default: True.
    """

    obligation_id: str
    recorded: bool
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        obligation_id = self.obligation_id

        recorded = self.recorded

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "obligation_id": obligation_id,
                "recorded": recorded,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        obligation_id = d.pop("obligation_id")

        recorded = d.pop("recorded")

        success = d.pop("success", UNSET)

        review_feedback_complete_response = cls(
            obligation_id=obligation_id,
            recorded=recorded,
            success=success,
        )

        review_feedback_complete_response.additional_properties = d
        return review_feedback_complete_response

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
