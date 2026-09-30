from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskRecoverRequest")


@_attrs_define
class TaskRecoverRequest:
    """
    Attributes:
        task_id (str): Task ID
        incident_id (str): Exact recovery incident ID from the supervisor notification
        decision (str):
        reason (str): Diagnosis and rationale for this decision
        expected_hold_at (float | None | Unset): Release a cleared hold by retrying this incident; exact decided_at of
            the prior hold. All recovery guards and remaining budgets still apply.
    """

    task_id: str
    incident_id: str
    decision: str
    reason: str
    expected_hold_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        incident_id = self.incident_id

        decision = self.decision

        reason = self.reason

        expected_hold_at: float | None | Unset
        if isinstance(self.expected_hold_at, Unset):
            expected_hold_at = UNSET
        else:
            expected_hold_at = self.expected_hold_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "incident_id": incident_id,
                "decision": decision,
                "reason": reason,
            }
        )
        if expected_hold_at is not UNSET:
            field_dict["expected_hold_at"] = expected_hold_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        incident_id = d.pop("incident_id")

        decision = d.pop("decision")

        reason = d.pop("reason")

        def _parse_expected_hold_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        expected_hold_at = _parse_expected_hold_at(d.pop("expected_hold_at", UNSET))

        task_recover_request = cls(
            task_id=task_id,
            incident_id=incident_id,
            decision=decision,
            reason=reason,
            expected_hold_at=expected_hold_at,
        )

        task_recover_request.additional_properties = d
        return task_recover_request

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
