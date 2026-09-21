from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReviewWithdrawResponse")


@_attrs_define
class ReviewWithdrawResponse:
    """
    Attributes:
        review_id (str):
        success (bool | Unset):  Default: True.
        flagged_task_ids (list[str] | Unset):
    """

    review_id: str
    success: bool | Unset = True
    flagged_task_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        review_id = self.review_id

        success = self.success

        flagged_task_ids: list[str] | Unset = UNSET
        if not isinstance(self.flagged_task_ids, Unset):
            flagged_task_ids = self.flagged_task_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "review_id": review_id,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if flagged_task_ids is not UNSET:
            field_dict["flagged_task_ids"] = flagged_task_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        review_id = d.pop("review_id")

        success = d.pop("success", UNSET)

        flagged_task_ids = cast(list[str], d.pop("flagged_task_ids", UNSET))

        review_withdraw_response = cls(
            review_id=review_id,
            success=success,
            flagged_task_ids=flagged_task_ids,
        )

        review_withdraw_response.additional_properties = d
        return review_withdraw_response

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
