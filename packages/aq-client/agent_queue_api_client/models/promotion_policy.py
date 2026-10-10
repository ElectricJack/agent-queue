from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.promotion_policy_flow_item import PromotionPolicyFlowItem


T = TypeVar("T", bound="PromotionPolicy")


@_attrs_define
class PromotionPolicy:
    """
    Attributes:
        flow (list[PromotionPolicyFlowItem]):
        type_ (Literal['promotion_flow'] | Unset):  Default: 'promotion_flow'.
    """

    flow: list[PromotionPolicyFlowItem]
    type_: Literal["promotion_flow"] | Unset = "promotion_flow"

    def to_dict(self) -> dict[str, Any]:
        flow = []
        for flow_item_data in self.flow:
            flow_item = flow_item_data.to_dict()
            flow.append(flow_item)

        type_ = self.type_

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "flow": flow,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.promotion_policy_flow_item import PromotionPolicyFlowItem

        d = dict(src_dict)
        flow = []
        _flow = d.pop("flow")
        for flow_item_data in _flow:
            flow_item = PromotionPolicyFlowItem.from_dict(flow_item_data)

            flow.append(flow_item)

        type_ = cast(Literal["promotion_flow"] | Unset, d.pop("type", UNSET))
        if type_ != "promotion_flow" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'promotion_flow', got '{type_}'")

        promotion_policy = cls(
            flow=flow,
            type_=type_,
        )

        return promotion_policy
