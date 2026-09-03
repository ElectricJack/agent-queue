from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.playbook_v1_drain_status_response_admission import PlaybookV1DrainStatusResponseAdmission
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.v1_run_summary_dto import V1RunSummaryDTO


T = TypeVar("T", bound="PlaybookV1DrainStatusResponse")


@_attrs_define
class PlaybookV1DrainStatusResponse:
    """``drained`` is a conjunction, deliberately: admission closed *and* no
    active run.  A zero count on its own is a snapshot — a run can start
    immediately after it is read — so it is never the gate by itself.

        Attributes:
            generated_at (float):
            admission (PlaybookV1DrainStatusResponseAdmission):
            live_count (int):
            orphaned_count (int):
            drained (bool):
            success (bool | Unset):  Default: True.
            closed_at (float | None | Unset):
            closed_by (None | str | Unset):
            active (list[V1RunSummaryDTO] | Unset):
            oldest_age_seconds (float | None | Unset):
            error (None | str | Unset):
    """

    generated_at: float
    admission: PlaybookV1DrainStatusResponseAdmission
    live_count: int
    orphaned_count: int
    drained: bool
    success: bool | Unset = True
    closed_at: float | None | Unset = UNSET
    closed_by: None | str | Unset = UNSET
    active: list[V1RunSummaryDTO] | Unset = UNSET
    oldest_age_seconds: float | None | Unset = UNSET
    error: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        generated_at = self.generated_at

        admission = self.admission.value

        live_count = self.live_count

        orphaned_count = self.orphaned_count

        drained = self.drained

        success = self.success

        closed_at: float | None | Unset
        if isinstance(self.closed_at, Unset):
            closed_at = UNSET
        else:
            closed_at = self.closed_at

        closed_by: None | str | Unset
        if isinstance(self.closed_by, Unset):
            closed_by = UNSET
        else:
            closed_by = self.closed_by

        active: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.active, Unset):
            active = []
            for active_item_data in self.active:
                active_item = active_item_data.to_dict()
                active.append(active_item)

        oldest_age_seconds: float | None | Unset
        if isinstance(self.oldest_age_seconds, Unset):
            oldest_age_seconds = UNSET
        else:
            oldest_age_seconds = self.oldest_age_seconds

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "generated_at": generated_at,
                "admission": admission,
                "live_count": live_count,
                "orphaned_count": orphaned_count,
                "drained": drained,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if closed_at is not UNSET:
            field_dict["closed_at"] = closed_at
        if closed_by is not UNSET:
            field_dict["closed_by"] = closed_by
        if active is not UNSET:
            field_dict["active"] = active
        if oldest_age_seconds is not UNSET:
            field_dict["oldest_age_seconds"] = oldest_age_seconds
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.v1_run_summary_dto import V1RunSummaryDTO

        d = dict(src_dict)
        generated_at = d.pop("generated_at")

        admission = PlaybookV1DrainStatusResponseAdmission(d.pop("admission"))

        live_count = d.pop("live_count")

        orphaned_count = d.pop("orphaned_count")

        drained = d.pop("drained")

        success = d.pop("success", UNSET)

        def _parse_closed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        closed_at = _parse_closed_at(d.pop("closed_at", UNSET))

        def _parse_closed_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        closed_by = _parse_closed_by(d.pop("closed_by", UNSET))

        _active = d.pop("active", UNSET)
        active: list[V1RunSummaryDTO] | Unset = UNSET
        if _active is not UNSET:
            active = []
            for active_item_data in _active:
                active_item = V1RunSummaryDTO.from_dict(active_item_data)

                active.append(active_item)

        def _parse_oldest_age_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        oldest_age_seconds = _parse_oldest_age_seconds(d.pop("oldest_age_seconds", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_v1_drain_status_response = cls(
            generated_at=generated_at,
            admission=admission,
            live_count=live_count,
            orphaned_count=orphaned_count,
            drained=drained,
            success=success,
            closed_at=closed_at,
            closed_by=closed_by,
            active=active,
            oldest_age_seconds=oldest_age_seconds,
            error=error,
        )

        return playbook_v1_drain_status_response
