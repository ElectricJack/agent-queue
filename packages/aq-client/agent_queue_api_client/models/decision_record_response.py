from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.operator_decision_record import OperatorDecisionRecord


T = TypeVar("T", bound="DecisionRecordResponse")


@_attrs_define
class DecisionRecordResponse:
    """
    Attributes:
        decision (OperatorDecisionRecord): One ``operator_decisions`` row; ``active`` is present on history reads.
        success (bool | Unset):  Default: True.
    """

    decision: OperatorDecisionRecord
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        decision = self.decision.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "decision": decision,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operator_decision_record import OperatorDecisionRecord

        d = dict(src_dict)
        decision = OperatorDecisionRecord.from_dict(d.pop("decision"))

        success = d.pop("success", UNSET)

        decision_record_response = cls(
            decision=decision,
            success=success,
        )

        return decision_record_response
