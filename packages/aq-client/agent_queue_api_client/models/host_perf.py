from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.pressure_perf import PressurePerf
    from ..models.test_slots_perf import TestSlotsPerf
    from ..models.ungated_perf import UngatedPerf


T = TypeVar("T", bound="HostPerf")


@_attrs_define
class HostPerf:
    """Slow-tier HostSampler snapshot, including its cost and staleness.

    Attributes:
        psi (PressurePerf | Unset): HostSampler's kernel pressure readings, or null with a reason.
        test_slots (TestSlotsPerf | Unset): Test-slot occupancy from the host reader's resource semaphore snapshot.
        ungated (UngatedPerf | Unset): Host reader's process attribution counts and bounded worktree slot names.
        host_ms (float | None | Unset):
        stale (bool | Unset):  Default: False.
        reason (None | str | Unset):
    """

    psi: PressurePerf | Unset = UNSET
    test_slots: TestSlotsPerf | Unset = UNSET
    ungated: UngatedPerf | Unset = UNSET
    host_ms: float | None | Unset = UNSET
    stale: bool | Unset = False
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        psi: dict[str, Any] | Unset = UNSET
        if not isinstance(self.psi, Unset):
            psi = self.psi.to_dict()

        test_slots: dict[str, Any] | Unset = UNSET
        if not isinstance(self.test_slots, Unset):
            test_slots = self.test_slots.to_dict()

        ungated: dict[str, Any] | Unset = UNSET
        if not isinstance(self.ungated, Unset):
            ungated = self.ungated.to_dict()

        host_ms: float | None | Unset
        if isinstance(self.host_ms, Unset):
            host_ms = UNSET
        else:
            host_ms = self.host_ms

        stale = self.stale

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if psi is not UNSET:
            field_dict["psi"] = psi
        if test_slots is not UNSET:
            field_dict["test_slots"] = test_slots
        if ungated is not UNSET:
            field_dict["ungated"] = ungated
        if host_ms is not UNSET:
            field_dict["host_ms"] = host_ms
        if stale is not UNSET:
            field_dict["stale"] = stale
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.pressure_perf import PressurePerf
        from ..models.test_slots_perf import TestSlotsPerf
        from ..models.ungated_perf import UngatedPerf

        d = dict(src_dict)
        _psi = d.pop("psi", UNSET)
        psi: PressurePerf | Unset
        if isinstance(_psi, Unset):
            psi = UNSET
        else:
            psi = PressurePerf.from_dict(_psi)

        _test_slots = d.pop("test_slots", UNSET)
        test_slots: TestSlotsPerf | Unset
        if isinstance(_test_slots, Unset):
            test_slots = UNSET
        else:
            test_slots = TestSlotsPerf.from_dict(_test_slots)

        _ungated = d.pop("ungated", UNSET)
        ungated: UngatedPerf | Unset
        if isinstance(_ungated, Unset):
            ungated = UNSET
        else:
            ungated = UngatedPerf.from_dict(_ungated)

        def _parse_host_ms(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        host_ms = _parse_host_ms(d.pop("host_ms", UNSET))

        stale = d.pop("stale", UNSET)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        host_perf = cls(
            psi=psi,
            test_slots=test_slots,
            ungated=ungated,
            host_ms=host_ms,
            stale=stale,
            reason=reason,
        )

        host_perf.additional_properties = d
        return host_perf

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
