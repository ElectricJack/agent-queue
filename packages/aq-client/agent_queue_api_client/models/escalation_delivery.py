from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="EscalationDelivery")


@_attrs_define
class EscalationDelivery:
    """
    Attributes:
        id (str):
        escalation_id (str):
        status (str):
        kind (str):
        attempt_count (int):
        generation (int):
    """

    id: str
    escalation_id: str
    status: str
    kind: str
    attempt_count: int
    generation: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        escalation_id = self.escalation_id

        status = self.status

        kind = self.kind

        attempt_count = self.attempt_count

        generation = self.generation

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "escalation_id": escalation_id,
                "status": status,
                "kind": kind,
                "attempt_count": attempt_count,
                "generation": generation,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        escalation_id = d.pop("escalation_id")

        status = d.pop("status")

        kind = d.pop("kind")

        attempt_count = d.pop("attempt_count")

        generation = d.pop("generation")

        escalation_delivery = cls(
            id=id,
            escalation_id=escalation_id,
            status=status,
            kind=kind,
            attempt_count=attempt_count,
            generation=generation,
        )

        escalation_delivery.additional_properties = d
        return escalation_delivery

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
