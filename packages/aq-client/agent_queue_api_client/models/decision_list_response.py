from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.operator_decision_record import OperatorDecisionRecord


T = TypeVar("T", bound="DecisionListResponse")


@_attrs_define
class DecisionListResponse:
    """
    Attributes:
        operator_decisions (list[OperatorDecisionRecord]):
        success (bool | Unset):  Default: True.
    """

    operator_decisions: list[OperatorDecisionRecord]
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        operator_decisions = []
        for operator_decisions_item_data in self.operator_decisions:
            operator_decisions_item = operator_decisions_item_data.to_dict()
            operator_decisions.append(operator_decisions_item)

        success = self.success

        field_dict: dict[str, Any] = {}

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
        from ..models.operator_decision_record import OperatorDecisionRecord

        d = dict(src_dict)
        operator_decisions = []
        _operator_decisions = d.pop("operator_decisions")
        for operator_decisions_item_data in _operator_decisions:
            operator_decisions_item = OperatorDecisionRecord.from_dict(operator_decisions_item_data)

            operator_decisions.append(operator_decisions_item)

        success = d.pop("success", UNSET)

        decision_list_response = cls(
            operator_decisions=operator_decisions,
            success=success,
        )

        return decision_list_response
