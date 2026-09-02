from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskMetrics")


@_attrs_define
class TaskMetrics:
    """
    Attributes:
        ready (float | Unset):  Default: 0.0.
        in_progress (float | Unset):  Default: 0.0.
        assigned (float | Unset):  Default: 0.0.
        paused (float | Unset):  Default: 0.0.
        blocked (float | Unset):  Default: 0.0.
        waiting_input (float | Unset):  Default: 0.0.
        other (float | Unset):  Default: 0.0.
        total (float | Unset):  Default: 0.0.
    """

    ready: float | Unset = 0.0
    in_progress: float | Unset = 0.0
    assigned: float | Unset = 0.0
    paused: float | Unset = 0.0
    blocked: float | Unset = 0.0
    waiting_input: float | Unset = 0.0
    other: float | Unset = 0.0
    total: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ready = self.ready

        in_progress = self.in_progress

        assigned = self.assigned

        paused = self.paused

        blocked = self.blocked

        waiting_input = self.waiting_input

        other = self.other

        total = self.total

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if ready is not UNSET:
            field_dict["READY"] = ready
        if in_progress is not UNSET:
            field_dict["IN_PROGRESS"] = in_progress
        if assigned is not UNSET:
            field_dict["ASSIGNED"] = assigned
        if paused is not UNSET:
            field_dict["PAUSED"] = paused
        if blocked is not UNSET:
            field_dict["BLOCKED"] = blocked
        if waiting_input is not UNSET:
            field_dict["WAITING_INPUT"] = waiting_input
        if other is not UNSET:
            field_dict["other"] = other
        if total is not UNSET:
            field_dict["total"] = total

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ready = d.pop("READY", UNSET)

        in_progress = d.pop("IN_PROGRESS", UNSET)

        assigned = d.pop("ASSIGNED", UNSET)

        paused = d.pop("PAUSED", UNSET)

        blocked = d.pop("BLOCKED", UNSET)

        waiting_input = d.pop("WAITING_INPUT", UNSET)

        other = d.pop("other", UNSET)

        total = d.pop("total", UNSET)

        task_metrics = cls(
            ready=ready,
            in_progress=in_progress,
            assigned=assigned,
            paused=paused,
            blocked=blocked,
            waiting_input=waiting_input,
            other=other,
            total=total,
        )

        task_metrics.additional_properties = d
        return task_metrics

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
