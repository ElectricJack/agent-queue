from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.review_dispatch_response_dispatches_item import ReviewDispatchResponseDispatchesItem


T = TypeVar("T", bound="ReviewDispatchResponse")


@_attrs_define
class ReviewDispatchResponse:
    """
    Attributes:
        review_id (str):
        dispatches (list[ReviewDispatchResponseDispatchesItem]):
        success (bool | Unset):  Default: True.
    """

    review_id: str
    dispatches: list[ReviewDispatchResponseDispatchesItem]
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        review_id = self.review_id

        dispatches = []
        for dispatches_item_data in self.dispatches:
            dispatches_item = dispatches_item_data.to_dict()
            dispatches.append(dispatches_item)

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "review_id": review_id,
                "dispatches": dispatches,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.review_dispatch_response_dispatches_item import ReviewDispatchResponseDispatchesItem

        d = dict(src_dict)
        review_id = d.pop("review_id")

        dispatches = []
        _dispatches = d.pop("dispatches")
        for dispatches_item_data in _dispatches:
            dispatches_item = ReviewDispatchResponseDispatchesItem.from_dict(dispatches_item_data)

            dispatches.append(dispatches_item)

        success = d.pop("success", UNSET)

        review_dispatch_response = cls(
            review_id=review_id,
            dispatches=dispatches,
            success=success,
        )

        review_dispatch_response.additional_properties = d
        return review_dispatch_response

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
