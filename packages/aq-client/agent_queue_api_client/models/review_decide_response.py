from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReviewDecideResponse")


@_attrs_define
class ReviewDecideResponse:
    """
    Attributes:
        review_id (str):
        state (str):
        success (bool | Unset):  Default: True.
        unblocked_task_ids (list[str] | Unset):
    """

    review_id: str
    state: str
    success: bool | Unset = True
    unblocked_task_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        review_id = self.review_id

        state = self.state

        success = self.success

        unblocked_task_ids: list[str] | Unset = UNSET
        if not isinstance(self.unblocked_task_ids, Unset):
            unblocked_task_ids = self.unblocked_task_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "review_id": review_id,
                "state": state,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if unblocked_task_ids is not UNSET:
            field_dict["unblocked_task_ids"] = unblocked_task_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        review_id = d.pop("review_id")

        state = d.pop("state")

        success = d.pop("success", UNSET)

        unblocked_task_ids = cast(list[str], d.pop("unblocked_task_ids", UNSET))

        review_decide_response = cls(
            review_id=review_id,
            state=state,
            success=success,
            unblocked_task_ids=unblocked_task_ids,
        )

        review_decide_response.additional_properties = d
        return review_decide_response

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
