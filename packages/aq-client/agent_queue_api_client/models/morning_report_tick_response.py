from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MorningReportTickResponse")


@_attrs_define
class MorningReportTickResponse:
    """
    Attributes:
        report_id (None | str):
        state (str):
        reason (None | str):
        next_due_at (float | None):
        cancelled (int):
        success (bool | Unset):  Default: True.
    """

    report_id: None | str
    state: str
    reason: None | str
    next_due_at: float | None
    cancelled: int
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        report_id: None | str
        report_id = self.report_id

        state = self.state

        reason: None | str
        reason = self.reason

        next_due_at: float | None
        next_due_at = self.next_due_at

        cancelled = self.cancelled

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "report_id": report_id,
                "state": state,
                "reason": reason,
                "next_due_at": next_due_at,
                "cancelled": cancelled,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_report_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        report_id = _parse_report_id(d.pop("report_id"))

        state = d.pop("state")

        def _parse_reason(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        reason = _parse_reason(d.pop("reason"))

        def _parse_next_due_at(data: object) -> float | None:
            if data is None:
                return data
            return cast(float | None, data)

        next_due_at = _parse_next_due_at(d.pop("next_due_at"))

        cancelled = d.pop("cancelled")

        success = d.pop("success", UNSET)

        morning_report_tick_response = cls(
            report_id=report_id,
            state=state,
            reason=reason,
            next_due_at=next_due_at,
            cancelled=cancelled,
            success=success,
        )

        morning_report_tick_response.additional_properties = d
        return morning_report_tick_response

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
