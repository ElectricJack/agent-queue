from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.operator_decision_model import OperatorDecisionModel


T = TypeVar("T", bound="DecisionListResponse")


@_attrs_define
class DecisionListResponse:
    """
    Attributes:
        operator_decisions (list[OperatorDecisionModel]):
        success (bool | Unset):  Default: True.
    """

    operator_decisions: list[OperatorDecisionModel]
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        operator_decisions = []
        for operator_decisions_item_data in self.operator_decisions:
            operator_decisions_item = operator_decisions_item_data.to_dict()
            operator_decisions.append(operator_decisions_item)

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "operator_decisions": operator_decisions,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operator_decision_model import OperatorDecisionModel

        d = dict(src_dict)
        operator_decisions = []
        _operator_decisions = d.pop("operator_decisions")
        for operator_decisions_item_data in _operator_decisions:
            operator_decisions_item = OperatorDecisionModel.from_dict(operator_decisions_item_data)

            operator_decisions.append(operator_decisions_item)

        success = d.pop("success", UNSET)

        decision_list_response = cls(
            operator_decisions=operator_decisions,
            success=success,
        )

        decision_list_response.additional_properties = d
        return decision_list_response

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
