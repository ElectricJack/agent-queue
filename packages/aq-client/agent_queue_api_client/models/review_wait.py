from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="ReviewWait")


@_attrs_define
class ReviewWait:
    """A document review one of this task's gates is tied to.

    ``blocking`` is the ``blocked_gate`` test ``aq task explain`` applies: the
    review's gate is attached to the task and not resolved (open or expired).
    A review gate is resolved only by an approval, so a rejected or withdrawn
    review still blocks.  A non-blocking entry is an approved review whose
    gate released the task, reported while the task is not yet completed so
    the card can still link to the design it was gated on.

        Attributes:
            review_id (str):
            review_state (str):
            review_kind (str):
            review_title (str):
            gate_id (str):
            gate_type (str):
            gate_status (str):
            blocking (bool):
    """

    review_id: str
    review_state: str
    review_kind: str
    review_title: str
    gate_id: str
    gate_type: str
    gate_status: str
    blocking: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        review_id = self.review_id

        review_state = self.review_state

        review_kind = self.review_kind

        review_title = self.review_title

        gate_id = self.gate_id

        gate_type = self.gate_type

        gate_status = self.gate_status

        blocking = self.blocking

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "review_id": review_id,
                "review_state": review_state,
                "review_kind": review_kind,
                "review_title": review_title,
                "gate_id": gate_id,
                "gate_type": gate_type,
                "gate_status": gate_status,
                "blocking": blocking,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        review_id = d.pop("review_id")

        review_state = d.pop("review_state")

        review_kind = d.pop("review_kind")

        review_title = d.pop("review_title")

        gate_id = d.pop("gate_id")

        gate_type = d.pop("gate_type")

        gate_status = d.pop("gate_status")

        blocking = d.pop("blocking")

        review_wait = cls(
            review_id=review_id,
            review_state=review_state,
            review_kind=review_kind,
            review_title=review_title,
            gate_id=gate_id,
            gate_type=gate_type,
            gate_status=gate_status,
            blocking=blocking,
        )

        review_wait.additional_properties = d
        return review_wait

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
