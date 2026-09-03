from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="CutoverWindowDTO")


@_attrs_define
class CutoverWindowDTO:
    """
    Attributes:
        wall_clock_gate_seconds (float):
        volume_gate_runs (int):
        switched_at (float | None | Unset):
        elapsed_seconds (float | None | Unset):
        wall_clock_ok (bool | Unset):  Default: False.
        closed_at (float | None | Unset):
    """

    wall_clock_gate_seconds: float
    volume_gate_runs: int
    switched_at: float | None | Unset = UNSET
    elapsed_seconds: float | None | Unset = UNSET
    wall_clock_ok: bool | Unset = False
    closed_at: float | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        wall_clock_gate_seconds = self.wall_clock_gate_seconds

        volume_gate_runs = self.volume_gate_runs

        switched_at: float | None | Unset
        if isinstance(self.switched_at, Unset):
            switched_at = UNSET
        else:
            switched_at = self.switched_at

        elapsed_seconds: float | None | Unset
        if isinstance(self.elapsed_seconds, Unset):
            elapsed_seconds = UNSET
        else:
            elapsed_seconds = self.elapsed_seconds

        wall_clock_ok = self.wall_clock_ok

        closed_at: float | None | Unset
        if isinstance(self.closed_at, Unset):
            closed_at = UNSET
        else:
            closed_at = self.closed_at

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "wall_clock_gate_seconds": wall_clock_gate_seconds,
                "volume_gate_runs": volume_gate_runs,
            }
        )
        if switched_at is not UNSET:
            field_dict["switched_at"] = switched_at
        if elapsed_seconds is not UNSET:
            field_dict["elapsed_seconds"] = elapsed_seconds
        if wall_clock_ok is not UNSET:
            field_dict["wall_clock_ok"] = wall_clock_ok
        if closed_at is not UNSET:
            field_dict["closed_at"] = closed_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        wall_clock_gate_seconds = d.pop("wall_clock_gate_seconds")

        volume_gate_runs = d.pop("volume_gate_runs")

        def _parse_switched_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        switched_at = _parse_switched_at(d.pop("switched_at", UNSET))

        def _parse_elapsed_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        elapsed_seconds = _parse_elapsed_seconds(d.pop("elapsed_seconds", UNSET))

        wall_clock_ok = d.pop("wall_clock_ok", UNSET)

        def _parse_closed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        closed_at = _parse_closed_at(d.pop("closed_at", UNSET))

        cutover_window_dto = cls(
            wall_clock_gate_seconds=wall_clock_gate_seconds,
            volume_gate_runs=volume_gate_runs,
            switched_at=switched_at,
            elapsed_seconds=elapsed_seconds,
            wall_clock_ok=wall_clock_ok,
            closed_at=closed_at,
        )

        return cutover_window_dto
