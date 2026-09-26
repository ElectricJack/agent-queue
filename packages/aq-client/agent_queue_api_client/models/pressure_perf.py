from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.pressure_entry import PressureEntry


T = TypeVar("T", bound="PressurePerf")


@_attrs_define
class PressurePerf:
    """HostSampler's kernel pressure readings, or null with a reason.

    Attributes:
        cpu (None | PressureEntry | Unset):
        io (None | PressureEntry | Unset):
        memory (None | PressureEntry | Unset):
        reason (None | str | Unset):
    """

    cpu: None | PressureEntry | Unset = UNSET
    io: None | PressureEntry | Unset = UNSET
    memory: None | PressureEntry | Unset = UNSET
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.pressure_entry import PressureEntry

        cpu: dict[str, Any] | None | Unset
        if isinstance(self.cpu, Unset):
            cpu = UNSET
        elif isinstance(self.cpu, PressureEntry):
            cpu = self.cpu.to_dict()
        else:
            cpu = self.cpu

        io: dict[str, Any] | None | Unset
        if isinstance(self.io, Unset):
            io = UNSET
        elif isinstance(self.io, PressureEntry):
            io = self.io.to_dict()
        else:
            io = self.io

        memory: dict[str, Any] | None | Unset
        if isinstance(self.memory, Unset):
            memory = UNSET
        elif isinstance(self.memory, PressureEntry):
            memory = self.memory.to_dict()
        else:
            memory = self.memory

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if cpu is not UNSET:
            field_dict["cpu"] = cpu
        if io is not UNSET:
            field_dict["io"] = io
        if memory is not UNSET:
            field_dict["memory"] = memory
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.pressure_entry import PressureEntry

        d = dict(src_dict)

        def _parse_cpu(data: object) -> None | PressureEntry | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cpu_type_0 = PressureEntry.from_dict(data)

                return cpu_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PressureEntry | Unset, data)

        cpu = _parse_cpu(d.pop("cpu", UNSET))

        def _parse_io(data: object) -> None | PressureEntry | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                io_type_0 = PressureEntry.from_dict(data)

                return io_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PressureEntry | Unset, data)

        io = _parse_io(d.pop("io", UNSET))

        def _parse_memory(data: object) -> None | PressureEntry | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                memory_type_0 = PressureEntry.from_dict(data)

                return memory_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PressureEntry | Unset, data)

        memory = _parse_memory(d.pop("memory", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        pressure_perf = cls(
            cpu=cpu,
            io=io,
            memory=memory,
            reason=reason,
        )

        pressure_perf.additional_properties = d
        return pressure_perf

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
