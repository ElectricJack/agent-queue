from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.diff_item import DiffItem
    from ..models.policy_review_receipt import PolicyReviewReceipt


T = TypeVar("T", bound="PolicyApplyResponse")


@_attrs_define
class PolicyApplyResponse:
    """
    Attributes:
        applied (list[str]):
        items (list[DiffItem]):
        success (bool | Unset):  Default: True.
        reviews (list[PolicyReviewReceipt] | Unset):
        pending_configuration (list[str] | Unset):
        activated (bool | Unset):  Default: False.
    """

    applied: list[str]
    items: list[DiffItem]
    success: bool | Unset = True
    reviews: list[PolicyReviewReceipt] | Unset = UNSET
    pending_configuration: list[str] | Unset = UNSET
    activated: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        applied = self.applied

        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        success = self.success

        reviews: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.reviews, Unset):
            reviews = []
            for reviews_item_data in self.reviews:
                reviews_item = reviews_item_data.to_dict()
                reviews.append(reviews_item)

        pending_configuration: list[str] | Unset = UNSET
        if not isinstance(self.pending_configuration, Unset):
            pending_configuration = self.pending_configuration

        activated = self.activated

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "applied": applied,
                "items": items,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if reviews is not UNSET:
            field_dict["reviews"] = reviews
        if pending_configuration is not UNSET:
            field_dict["pending_configuration"] = pending_configuration
        if activated is not UNSET:
            field_dict["activated"] = activated

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.diff_item import DiffItem
        from ..models.policy_review_receipt import PolicyReviewReceipt

        d = dict(src_dict)
        applied = cast(list[str], d.pop("applied"))

        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = DiffItem.from_dict(items_item_data)

            items.append(items_item)

        success = d.pop("success", UNSET)

        _reviews = d.pop("reviews", UNSET)
        reviews: list[PolicyReviewReceipt] | Unset = UNSET
        if _reviews is not UNSET:
            reviews = []
            for reviews_item_data in _reviews:
                reviews_item = PolicyReviewReceipt.from_dict(reviews_item_data)

                reviews.append(reviews_item)

        pending_configuration = cast(list[str], d.pop("pending_configuration", UNSET))

        activated = d.pop("activated", UNSET)

        policy_apply_response = cls(
            applied=applied,
            items=items,
            success=success,
            reviews=reviews,
            pending_configuration=pending_configuration,
            activated=activated,
        )

        policy_apply_response.additional_properties = d
        return policy_apply_response

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
