from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RejectPlanResponse")


@_attrs_define
class RejectPlanResponse:
    """
    Attributes:
        rejected (str):
        title (str):
        status (str | Unset):  Default: 'READY'.
        feedback_added (bool | Unset):  Default: False.
        draft_subtasks_deleted (int | Unset):  Default: 0.
    """

    rejected: str
    title: str
    status: str | Unset = "READY"
    feedback_added: bool | Unset = False
    draft_subtasks_deleted: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        rejected = self.rejected

        title = self.title

        status = self.status

        feedback_added = self.feedback_added

        draft_subtasks_deleted = self.draft_subtasks_deleted

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "rejected": rejected,
                "title": title,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if feedback_added is not UNSET:
            field_dict["feedback_added"] = feedback_added
        if draft_subtasks_deleted is not UNSET:
            field_dict["draft_subtasks_deleted"] = draft_subtasks_deleted

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        rejected = d.pop("rejected")

        title = d.pop("title")

        status = d.pop("status", UNSET)

        feedback_added = d.pop("feedback_added", UNSET)

        draft_subtasks_deleted = d.pop("draft_subtasks_deleted", UNSET)

        reject_plan_response = cls(
            rejected=rejected,
            title=title,
            status=status,
            feedback_added=feedback_added,
            draft_subtasks_deleted=draft_subtasks_deleted,
        )

        reject_plan_response.additional_properties = d
        return reject_plan_response

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
