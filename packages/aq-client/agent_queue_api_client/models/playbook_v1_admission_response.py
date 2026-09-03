from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.playbook_v1_admission_response_admission_type_0 import PlaybookV1AdmissionResponseAdmissionType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cutover_event_dto import CutoverEventDTO
    from ..models.v1_run_summary_dto import V1RunSummaryDTO


T = TypeVar("T", bound="PlaybookV1AdmissionResponse")


@_attrs_define
class PlaybookV1AdmissionResponse:
    """The drain snapshot as it stands after an admission change.

    Attributes:
        success (bool):
        event (CutoverEventDTO | None | Unset):
        generated_at (float | None | Unset):
        admission (None | PlaybookV1AdmissionResponseAdmissionType0 | Unset):
        closed_at (float | None | Unset):
        closed_by (None | str | Unset):
        active (list[V1RunSummaryDTO] | Unset):
        live_count (int | None | Unset):
        orphaned_count (int | None | Unset):
        oldest_age_seconds (float | None | Unset):
        drained (bool | None | Unset):
        error (None | str | Unset):
    """

    success: bool
    event: CutoverEventDTO | None | Unset = UNSET
    generated_at: float | None | Unset = UNSET
    admission: None | PlaybookV1AdmissionResponseAdmissionType0 | Unset = UNSET
    closed_at: float | None | Unset = UNSET
    closed_by: None | str | Unset = UNSET
    active: list[V1RunSummaryDTO] | Unset = UNSET
    live_count: int | None | Unset = UNSET
    orphaned_count: int | None | Unset = UNSET
    oldest_age_seconds: float | None | Unset = UNSET
    drained: bool | None | Unset = UNSET
    error: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.cutover_event_dto import CutoverEventDTO

        success = self.success

        event: dict[str, Any] | None | Unset
        if isinstance(self.event, Unset):
            event = UNSET
        elif isinstance(self.event, CutoverEventDTO):
            event = self.event.to_dict()
        else:
            event = self.event

        generated_at: float | None | Unset
        if isinstance(self.generated_at, Unset):
            generated_at = UNSET
        else:
            generated_at = self.generated_at

        admission: None | str | Unset
        if isinstance(self.admission, Unset):
            admission = UNSET
        elif isinstance(self.admission, PlaybookV1AdmissionResponseAdmissionType0):
            admission = self.admission.value
        else:
            admission = self.admission

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

        live_count: int | None | Unset
        if isinstance(self.live_count, Unset):
            live_count = UNSET
        else:
            live_count = self.live_count

        orphaned_count: int | None | Unset
        if isinstance(self.orphaned_count, Unset):
            orphaned_count = UNSET
        else:
            orphaned_count = self.orphaned_count

        oldest_age_seconds: float | None | Unset
        if isinstance(self.oldest_age_seconds, Unset):
            oldest_age_seconds = UNSET
        else:
            oldest_age_seconds = self.oldest_age_seconds

        drained: bool | None | Unset
        if isinstance(self.drained, Unset):
            drained = UNSET
        else:
            drained = self.drained

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "success": success,
            }
        )
        if event is not UNSET:
            field_dict["event"] = event
        if generated_at is not UNSET:
            field_dict["generated_at"] = generated_at
        if admission is not UNSET:
            field_dict["admission"] = admission
        if closed_at is not UNSET:
            field_dict["closed_at"] = closed_at
        if closed_by is not UNSET:
            field_dict["closed_by"] = closed_by
        if active is not UNSET:
            field_dict["active"] = active
        if live_count is not UNSET:
            field_dict["live_count"] = live_count
        if orphaned_count is not UNSET:
            field_dict["orphaned_count"] = orphaned_count
        if oldest_age_seconds is not UNSET:
            field_dict["oldest_age_seconds"] = oldest_age_seconds
        if drained is not UNSET:
            field_dict["drained"] = drained
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cutover_event_dto import CutoverEventDTO
        from ..models.v1_run_summary_dto import V1RunSummaryDTO

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_event(data: object) -> CutoverEventDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                event_type_0 = CutoverEventDTO.from_dict(data)

                return event_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CutoverEventDTO | None | Unset, data)

        event = _parse_event(d.pop("event", UNSET))

        def _parse_generated_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        generated_at = _parse_generated_at(d.pop("generated_at", UNSET))

        def _parse_admission(data: object) -> None | PlaybookV1AdmissionResponseAdmissionType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                admission_type_0 = PlaybookV1AdmissionResponseAdmissionType0(data)

                return admission_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookV1AdmissionResponseAdmissionType0 | Unset, data)

        admission = _parse_admission(d.pop("admission", UNSET))

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

        def _parse_live_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        live_count = _parse_live_count(d.pop("live_count", UNSET))

        def _parse_orphaned_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        orphaned_count = _parse_orphaned_count(d.pop("orphaned_count", UNSET))

        def _parse_oldest_age_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        oldest_age_seconds = _parse_oldest_age_seconds(d.pop("oldest_age_seconds", UNSET))

        def _parse_drained(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        drained = _parse_drained(d.pop("drained", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_v1_admission_response = cls(
            success=success,
            event=event,
            generated_at=generated_at,
            admission=admission,
            closed_at=closed_at,
            closed_by=closed_by,
            active=active,
            live_count=live_count,
            orphaned_count=orphaned_count,
            oldest_age_seconds=oldest_age_seconds,
            drained=drained,
            error=error,
        )

        return playbook_v1_admission_response
