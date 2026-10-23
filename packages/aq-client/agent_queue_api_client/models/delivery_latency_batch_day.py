from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeliveryLatencyBatchDay")


@_attrs_define
class DeliveryLatencyBatchDay:
    """Integration batches created on one UTC day (``day`` is its epoch start).

    Attributes:
        day (float):
        batches (int | Unset):  Default: 0.
        members (int | Unset):  Default: 0.
        red_rounds (int | Unset):  Default: 0.
    """

    day: float
    batches: int | Unset = 0
    members: int | Unset = 0
    red_rounds: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        day = self.day

        batches = self.batches

        members = self.members

        red_rounds = self.red_rounds

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "day": day,
            }
        )
        if batches is not UNSET:
            field_dict["batches"] = batches
        if members is not UNSET:
            field_dict["members"] = members
        if red_rounds is not UNSET:
            field_dict["red_rounds"] = red_rounds

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        day = d.pop("day")

        batches = d.pop("batches", UNSET)

        members = d.pop("members", UNSET)

        red_rounds = d.pop("red_rounds", UNSET)

        delivery_latency_batch_day = cls(
            day=day,
            batches=batches,
            members=members,
            red_rounds=red_rounds,
        )

        delivery_latency_batch_day.additional_properties = d
        return delivery_latency_batch_day

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
