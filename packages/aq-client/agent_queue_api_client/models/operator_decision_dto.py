from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..models.operator_decision_dto_options_item import OperatorDecisionDTOOptionsItem
from ..types import UNSET, Unset

T = TypeVar("T", bound="OperatorDecisionDTO")


@_attrs_define
class OperatorDecisionDTO:
    """A run paused with ``operator_decision_required`` after an ambiguous
    interruption of a non-retry-safe command (design spec, run-state §).

        Attributes:
            step_id (str):
            attempt (int):
            reason (str):
            raised_at (float):
            options (list[OperatorDecisionDTOOptionsItem] | Unset):
    """

    step_id: str
    attempt: int
    reason: str
    raised_at: float
    options: list[OperatorDecisionDTOOptionsItem] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        step_id = self.step_id

        attempt = self.attempt

        reason = self.reason

        raised_at = self.raised_at

        options: list[str] | Unset = UNSET
        if not isinstance(self.options, Unset):
            options = []
            for options_item_data in self.options:
                options_item = options_item_data.value
                options.append(options_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "step_id": step_id,
                "attempt": attempt,
                "reason": reason,
                "raised_at": raised_at,
            }
        )
        if options is not UNSET:
            field_dict["options"] = options

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        step_id = d.pop("step_id")

        attempt = d.pop("attempt")

        reason = d.pop("reason")

        raised_at = d.pop("raised_at")

        _options = d.pop("options", UNSET)
        options: list[OperatorDecisionDTOOptionsItem] | Unset = UNSET
        if _options is not UNSET:
            options = []
            for options_item_data in _options:
                options_item = OperatorDecisionDTOOptionsItem(options_item_data)

                options.append(options_item)

        operator_decision_dto = cls(
            step_id=step_id,
            attempt=attempt,
            reason=reason,
            raised_at=raised_at,
            options=options,
        )

        return operator_decision_dto
