from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.test_selection_promote_response_promotion import TestSelectionPromoteResponsePromotion


T = TypeVar("T", bound="TestSelectionPromoteResponse")


@_attrs_define
class TestSelectionPromoteResponse:
    """
    Attributes:
        promotion (TestSelectionPromoteResponsePromotion):
        success (bool | Unset):  Default: True.
    """

    promotion: TestSelectionPromoteResponsePromotion
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        promotion = self.promotion.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "promotion": promotion,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.test_selection_promote_response_promotion import TestSelectionPromoteResponsePromotion

        d = dict(src_dict)
        promotion = TestSelectionPromoteResponsePromotion.from_dict(d.pop("promotion"))

        success = d.pop("success", UNSET)

        test_selection_promote_response = cls(
            promotion=promotion,
            success=success,
        )

        test_selection_promote_response.additional_properties = d
        return test_selection_promote_response

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
