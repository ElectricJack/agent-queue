from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskRecoveryResponse")


@_attrs_define
class TaskRecoveryResponse:
    """
    Attributes:
        task_id (str):
        incident_id (str):
        decision (str):
        status (str):
        decided_at (float | None | Unset):
    """

    task_id: str
    incident_id: str
    decision: str
    status: str
    decided_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        incident_id = self.incident_id

        decision = self.decision

        status = self.status

        decided_at: float | None | Unset
        if isinstance(self.decided_at, Unset):
            decided_at = UNSET
        else:
            decided_at = self.decided_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "incident_id": incident_id,
                "decision": decision,
                "status": status,
            }
        )
        if decided_at is not UNSET:
            field_dict["decided_at"] = decided_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        incident_id = d.pop("incident_id")

        decision = d.pop("decision")

        status = d.pop("status")

        def _parse_decided_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        decided_at = _parse_decided_at(d.pop("decided_at", UNSET))

        task_recovery_response = cls(
            task_id=task_id,
            incident_id=incident_id,
            decision=decision,
            status=status,
            decided_at=decided_at,
        )

        task_recovery_response.additional_properties = d
        return task_recovery_response

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
