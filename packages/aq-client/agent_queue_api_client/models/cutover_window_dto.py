from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cutover_window_dtov2_runs_by_playbook import CutoverWindowDTOV2RunsByPlaybook


T = TypeVar("T", bound="CutoverWindowDTO")


@_attrs_define
class CutoverWindowDTO:
    """The observation window (§3.5): wall clock, coverage and volume.

    ``since``/``until``/``observed_at`` are the durable bounds every measure
    was read over — ``since`` is the ``switched_to_v2`` audit row's timestamp,
    never a clock the daemon could have restarted.

        Attributes:
            wall_clock_gate_seconds (float):
            volume_gate_runs (int):
            switched_at (float | None | Unset):
            since (float | None | Unset):
            until (float | None | Unset):
            observed_at (float | None | Unset):
            elapsed_seconds (float | None | Unset):
            wall_clock_ok (bool | Unset):  Default: False.
            coverage_ok (bool | Unset):  Default: False.
            coverage_missing (list[str] | Unset):
            enabled_playbooks (list[str] | Unset):
            volume_ok (bool | Unset):  Default: False.
            v2_run_count (int | Unset):  Default: 0.
            v2_runs_by_playbook (CutoverWindowDTOV2RunsByPlaybook | Unset):
            rehearsal_at (float | None | Unset):
            closed_at (float | None | Unset):
    """

    wall_clock_gate_seconds: float
    volume_gate_runs: int
    switched_at: float | None | Unset = UNSET
    since: float | None | Unset = UNSET
    until: float | None | Unset = UNSET
    observed_at: float | None | Unset = UNSET
    elapsed_seconds: float | None | Unset = UNSET
    wall_clock_ok: bool | Unset = False
    coverage_ok: bool | Unset = False
    coverage_missing: list[str] | Unset = UNSET
    enabled_playbooks: list[str] | Unset = UNSET
    volume_ok: bool | Unset = False
    v2_run_count: int | Unset = 0
    v2_runs_by_playbook: CutoverWindowDTOV2RunsByPlaybook | Unset = UNSET
    rehearsal_at: float | None | Unset = UNSET
    closed_at: float | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        wall_clock_gate_seconds = self.wall_clock_gate_seconds

        volume_gate_runs = self.volume_gate_runs

        switched_at: float | None | Unset
        if isinstance(self.switched_at, Unset):
            switched_at = UNSET
        else:
            switched_at = self.switched_at

        since: float | None | Unset
        if isinstance(self.since, Unset):
            since = UNSET
        else:
            since = self.since

        until: float | None | Unset
        if isinstance(self.until, Unset):
            until = UNSET
        else:
            until = self.until

        observed_at: float | None | Unset
        if isinstance(self.observed_at, Unset):
            observed_at = UNSET
        else:
            observed_at = self.observed_at

        elapsed_seconds: float | None | Unset
        if isinstance(self.elapsed_seconds, Unset):
            elapsed_seconds = UNSET
        else:
            elapsed_seconds = self.elapsed_seconds

        wall_clock_ok = self.wall_clock_ok

        coverage_ok = self.coverage_ok

        coverage_missing: list[str] | Unset = UNSET
        if not isinstance(self.coverage_missing, Unset):
            coverage_missing = self.coverage_missing

        enabled_playbooks: list[str] | Unset = UNSET
        if not isinstance(self.enabled_playbooks, Unset):
            enabled_playbooks = self.enabled_playbooks

        volume_ok = self.volume_ok

        v2_run_count = self.v2_run_count

        v2_runs_by_playbook: dict[str, Any] | Unset = UNSET
        if not isinstance(self.v2_runs_by_playbook, Unset):
            v2_runs_by_playbook = self.v2_runs_by_playbook.to_dict()

        rehearsal_at: float | None | Unset
        if isinstance(self.rehearsal_at, Unset):
            rehearsal_at = UNSET
        else:
            rehearsal_at = self.rehearsal_at

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
        if since is not UNSET:
            field_dict["since"] = since
        if until is not UNSET:
            field_dict["until"] = until
        if observed_at is not UNSET:
            field_dict["observed_at"] = observed_at
        if elapsed_seconds is not UNSET:
            field_dict["elapsed_seconds"] = elapsed_seconds
        if wall_clock_ok is not UNSET:
            field_dict["wall_clock_ok"] = wall_clock_ok
        if coverage_ok is not UNSET:
            field_dict["coverage_ok"] = coverage_ok
        if coverage_missing is not UNSET:
            field_dict["coverage_missing"] = coverage_missing
        if enabled_playbooks is not UNSET:
            field_dict["enabled_playbooks"] = enabled_playbooks
        if volume_ok is not UNSET:
            field_dict["volume_ok"] = volume_ok
        if v2_run_count is not UNSET:
            field_dict["v2_run_count"] = v2_run_count
        if v2_runs_by_playbook is not UNSET:
            field_dict["v2_runs_by_playbook"] = v2_runs_by_playbook
        if rehearsal_at is not UNSET:
            field_dict["rehearsal_at"] = rehearsal_at
        if closed_at is not UNSET:
            field_dict["closed_at"] = closed_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cutover_window_dtov2_runs_by_playbook import CutoverWindowDTOV2RunsByPlaybook

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

        def _parse_since(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        since = _parse_since(d.pop("since", UNSET))

        def _parse_until(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        until = _parse_until(d.pop("until", UNSET))

        def _parse_observed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        observed_at = _parse_observed_at(d.pop("observed_at", UNSET))

        def _parse_elapsed_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        elapsed_seconds = _parse_elapsed_seconds(d.pop("elapsed_seconds", UNSET))

        wall_clock_ok = d.pop("wall_clock_ok", UNSET)

        coverage_ok = d.pop("coverage_ok", UNSET)

        coverage_missing = cast(list[str], d.pop("coverage_missing", UNSET))

        enabled_playbooks = cast(list[str], d.pop("enabled_playbooks", UNSET))

        volume_ok = d.pop("volume_ok", UNSET)

        v2_run_count = d.pop("v2_run_count", UNSET)

        _v2_runs_by_playbook = d.pop("v2_runs_by_playbook", UNSET)
        v2_runs_by_playbook: CutoverWindowDTOV2RunsByPlaybook | Unset
        if isinstance(_v2_runs_by_playbook, Unset):
            v2_runs_by_playbook = UNSET
        else:
            v2_runs_by_playbook = CutoverWindowDTOV2RunsByPlaybook.from_dict(_v2_runs_by_playbook)

        def _parse_rehearsal_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        rehearsal_at = _parse_rehearsal_at(d.pop("rehearsal_at", UNSET))

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
            since=since,
            until=until,
            observed_at=observed_at,
            elapsed_seconds=elapsed_seconds,
            wall_clock_ok=wall_clock_ok,
            coverage_ok=coverage_ok,
            coverage_missing=coverage_missing,
            enabled_playbooks=enabled_playbooks,
            volume_ok=volume_ok,
            v2_run_count=v2_run_count,
            v2_runs_by_playbook=v2_runs_by_playbook,
            rehearsal_at=rehearsal_at,
            closed_at=closed_at,
        )

        return cutover_window_dto
