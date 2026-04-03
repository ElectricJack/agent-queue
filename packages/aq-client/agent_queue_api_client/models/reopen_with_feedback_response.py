from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReopenWithFeedbackResponse")


@_attrs_define
class ReopenWithFeedbackResponse:
    """
    Attributes:
        reopened (str):
        title (str):
        previous_status (str | Unset):  Default: ''.
        status (str | Unset):  Default: 'READY'.
        feedback_added (bool | Unset):  Default: False.
        requires_approval (bool | Unset):  Default: False.
    """

    reopened: str
    title: str
    previous_status: str | Unset = ""
    status: str | Unset = "READY"
    feedback_added: bool | Unset = False
    requires_approval: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        reopened = self.reopened

        title = self.title

        previous_status = self.previous_status

        status = self.status

        feedback_added = self.feedback_added

        requires_approval = self.requires_approval

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "reopened": reopened,
                "title": title,
            }
        )
        if previous_status is not UNSET:
            field_dict["previous_status"] = previous_status
        if status is not UNSET:
            field_dict["status"] = status
        if feedback_added is not UNSET:
            field_dict["feedback_added"] = feedback_added
        if requires_approval is not UNSET:
            field_dict["requires_approval"] = requires_approval

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        reopened = d.pop("reopened")

        title = d.pop("title")

        previous_status = d.pop("previous_status", UNSET)

        status = d.pop("status", UNSET)

        feedback_added = d.pop("feedback_added", UNSET)

        requires_approval = d.pop("requires_approval", UNSET)

        reopen_with_feedback_response = cls(
            reopened=reopened,
            title=title,
            previous_status=previous_status,
            status=status,
            feedback_added=feedback_added,
            requires_approval=requires_approval,
        )

        reopen_with_feedback_response.additional_properties = d
        return reopen_with_feedback_response

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
