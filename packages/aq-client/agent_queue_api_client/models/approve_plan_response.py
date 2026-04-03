from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.approve_plan_response_subtasks_item import ApprovePlanResponseSubtasksItem


T = TypeVar("T", bound="ApprovePlanResponse")


@_attrs_define
class ApprovePlanResponse:
    """
    Attributes:
        approved (str):
        title (str):
        subtask_count (int | Unset):  Default: 0.
        subtasks (list[ApprovePlanResponseSubtasksItem] | Unset):
    """

    approved: str
    title: str
    subtask_count: int | Unset = 0
    subtasks: list[ApprovePlanResponseSubtasksItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        approved = self.approved

        title = self.title

        subtask_count = self.subtask_count

        subtasks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.subtasks, Unset):
            subtasks = []
            for subtasks_item_data in self.subtasks:
                subtasks_item = subtasks_item_data.to_dict()
                subtasks.append(subtasks_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "approved": approved,
                "title": title,
            }
        )
        if subtask_count is not UNSET:
            field_dict["subtask_count"] = subtask_count
        if subtasks is not UNSET:
            field_dict["subtasks"] = subtasks

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.approve_plan_response_subtasks_item import ApprovePlanResponseSubtasksItem

        d = dict(src_dict)
        approved = d.pop("approved")

        title = d.pop("title")

        subtask_count = d.pop("subtask_count", UNSET)

        _subtasks = d.pop("subtasks", UNSET)
        subtasks: list[ApprovePlanResponseSubtasksItem] | Unset = UNSET
        if _subtasks is not UNSET:
            subtasks = []
            for subtasks_item_data in _subtasks:
                subtasks_item = ApprovePlanResponseSubtasksItem.from_dict(subtasks_item_data)

                subtasks.append(subtasks_item)

        approve_plan_response = cls(
            approved=approved,
            title=title,
            subtask_count=subtask_count,
            subtasks=subtasks,
        )

        approve_plan_response.additional_properties = d
        return approve_plan_response

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
