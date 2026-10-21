from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="ReviewFeedbackCompleteRequest")


@_attrs_define
class ReviewFeedbackCompleteRequest:
    """
    Attributes:
        task_id (str):
        obligation_id (str):
        evidence (str):
    """

    task_id: str
    obligation_id: str
    evidence: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        obligation_id = self.obligation_id

        evidence = self.evidence

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "obligation_id": obligation_id,
                "evidence": evidence,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        obligation_id = d.pop("obligation_id")

        evidence = d.pop("evidence")

        review_feedback_complete_request = cls(
            task_id=task_id,
            obligation_id=obligation_id,
            evidence=evidence,
        )

        review_feedback_complete_request.additional_properties = d
        return review_feedback_complete_request

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
