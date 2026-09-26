from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="UngatedPerf")


@_attrs_define
class UngatedPerf:
    """Host reader's process attribution counts and bounded worktree slot names.

    Attributes:
        pytest_processes (float | None | Unset):
        unattributed (float | None | Unset):
        unattributed_slots (list[str] | Unset):
    """

    pytest_processes: float | None | Unset = UNSET
    unattributed: float | None | Unset = UNSET
    unattributed_slots: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        pytest_processes: float | None | Unset
        if isinstance(self.pytest_processes, Unset):
            pytest_processes = UNSET
        else:
            pytest_processes = self.pytest_processes

        unattributed: float | None | Unset
        if isinstance(self.unattributed, Unset):
            unattributed = UNSET
        else:
            unattributed = self.unattributed

        unattributed_slots: list[str] | Unset = UNSET
        if not isinstance(self.unattributed_slots, Unset):
            unattributed_slots = self.unattributed_slots

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if pytest_processes is not UNSET:
            field_dict["pytest_processes"] = pytest_processes
        if unattributed is not UNSET:
            field_dict["unattributed"] = unattributed
        if unattributed_slots is not UNSET:
            field_dict["unattributed_slots"] = unattributed_slots

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_pytest_processes(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        pytest_processes = _parse_pytest_processes(d.pop("pytest_processes", UNSET))

        def _parse_unattributed(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        unattributed = _parse_unattributed(d.pop("unattributed", UNSET))

        unattributed_slots = cast(list[str], d.pop("unattributed_slots", UNSET))

        ungated_perf = cls(
            pytest_processes=pytest_processes,
            unattributed=unattributed,
            unattributed_slots=unattributed_slots,
        )

        ungated_perf.additional_properties = d
        return ungated_perf

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
