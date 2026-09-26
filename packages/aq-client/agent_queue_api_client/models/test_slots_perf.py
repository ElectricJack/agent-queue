from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TestSlotsPerf")


@_attrs_define
class TestSlotsPerf:
    """Test-slot occupancy from the host reader's resource semaphore snapshot.

    Attributes:
        used (float | None | Unset):
        total (float | None | Unset):
        waiting (float | None | Unset):
        orphaned (float | None | Unset):
        reason (None | str | Unset):
    """

    used: float | None | Unset = UNSET
    total: float | None | Unset = UNSET
    waiting: float | None | Unset = UNSET
    orphaned: float | None | Unset = UNSET
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        used: float | None | Unset
        if isinstance(self.used, Unset):
            used = UNSET
        else:
            used = self.used

        total: float | None | Unset
        if isinstance(self.total, Unset):
            total = UNSET
        else:
            total = self.total

        waiting: float | None | Unset
        if isinstance(self.waiting, Unset):
            waiting = UNSET
        else:
            waiting = self.waiting

        orphaned: float | None | Unset
        if isinstance(self.orphaned, Unset):
            orphaned = UNSET
        else:
            orphaned = self.orphaned

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if used is not UNSET:
            field_dict["used"] = used
        if total is not UNSET:
            field_dict["total"] = total
        if waiting is not UNSET:
            field_dict["waiting"] = waiting
        if orphaned is not UNSET:
            field_dict["orphaned"] = orphaned
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_used(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        used = _parse_used(d.pop("used", UNSET))

        def _parse_total(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        total = _parse_total(d.pop("total", UNSET))

        def _parse_waiting(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        waiting = _parse_waiting(d.pop("waiting", UNSET))

        def _parse_orphaned(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        orphaned = _parse_orphaned(d.pop("orphaned", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        test_slots_perf = cls(
            used=used,
            total=total,
            waiting=waiting,
            orphaned=orphaned,
            reason=reason,
        )

        test_slots_perf.additional_properties = d
        return test_slots_perf

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
