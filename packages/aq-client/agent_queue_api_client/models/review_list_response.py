from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.review_record import ReviewRecord


T = TypeVar("T", bound="ReviewListResponse")


@_attrs_define
class ReviewListResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        reviews (list[ReviewRecord] | Unset):
    """

    success: bool | Unset = True
    reviews: list[ReviewRecord] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        reviews: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.reviews, Unset):
            reviews = []
            for reviews_item_data in self.reviews:
                reviews_item = reviews_item_data.to_dict()
                reviews.append(reviews_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if reviews is not UNSET:
            field_dict["reviews"] = reviews

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.review_record import ReviewRecord

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _reviews = d.pop("reviews", UNSET)
        reviews: list[ReviewRecord] | Unset = UNSET
        if _reviews is not UNSET:
            reviews = []
            for reviews_item_data in _reviews:
                reviews_item = ReviewRecord.from_dict(reviews_item_data)

                reviews.append(reviews_item)

        review_list_response = cls(
            success=success,
            reviews=reviews,
        )

        review_list_response.additional_properties = d
        return review_list_response

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
