from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cutover_event_dto import CutoverEventDTO
    from ..models.cutover_window_dto import CutoverWindowDTO
    from ..models.playbook_cutover_window_close_response_measures_item import (
        PlaybookCutoverWindowCloseResponseMeasuresItem,
    )


T = TypeVar("T", bound="PlaybookCutoverWindowCloseResponse")


@_attrs_define
class PlaybookCutoverWindowCloseResponse:
    """
    Attributes:
        success (bool):
        event (CutoverEventDTO | None | Unset):
        blocking_reasons (list[str] | Unset):
        measures (list[PlaybookCutoverWindowCloseResponseMeasuresItem] | Unset):
        window (CutoverWindowDTO | None | Unset):
        evidence_errors (list[str] | Unset):
        error (None | str | Unset):
    """

    success: bool
    event: CutoverEventDTO | None | Unset = UNSET
    blocking_reasons: list[str] | Unset = UNSET
    measures: list[PlaybookCutoverWindowCloseResponseMeasuresItem] | Unset = UNSET
    window: CutoverWindowDTO | None | Unset = UNSET
    evidence_errors: list[str] | Unset = UNSET
    error: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.cutover_event_dto import CutoverEventDTO
        from ..models.cutover_window_dto import CutoverWindowDTO

        success = self.success

        event: dict[str, Any] | None | Unset
        if isinstance(self.event, Unset):
            event = UNSET
        elif isinstance(self.event, CutoverEventDTO):
            event = self.event.to_dict()
        else:
            event = self.event

        blocking_reasons: list[str] | Unset = UNSET
        if not isinstance(self.blocking_reasons, Unset):
            blocking_reasons = self.blocking_reasons

        measures: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.measures, Unset):
            measures = []
            for measures_item_data in self.measures:
                measures_item = measures_item_data.to_dict()
                measures.append(measures_item)

        window: dict[str, Any] | None | Unset
        if isinstance(self.window, Unset):
            window = UNSET
        elif isinstance(self.window, CutoverWindowDTO):
            window = self.window.to_dict()
        else:
            window = self.window

        evidence_errors: list[str] | Unset = UNSET
        if not isinstance(self.evidence_errors, Unset):
            evidence_errors = self.evidence_errors

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
        if blocking_reasons is not UNSET:
            field_dict["blocking_reasons"] = blocking_reasons
        if measures is not UNSET:
            field_dict["measures"] = measures
        if window is not UNSET:
            field_dict["window"] = window
        if evidence_errors is not UNSET:
            field_dict["evidence_errors"] = evidence_errors
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cutover_event_dto import CutoverEventDTO
        from ..models.cutover_window_dto import CutoverWindowDTO
        from ..models.playbook_cutover_window_close_response_measures_item import (
            PlaybookCutoverWindowCloseResponseMeasuresItem,
        )

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

        blocking_reasons = cast(list[str], d.pop("blocking_reasons", UNSET))

        _measures = d.pop("measures", UNSET)
        measures: list[PlaybookCutoverWindowCloseResponseMeasuresItem] | Unset = UNSET
        if _measures is not UNSET:
            measures = []
            for measures_item_data in _measures:
                measures_item = PlaybookCutoverWindowCloseResponseMeasuresItem.from_dict(measures_item_data)

                measures.append(measures_item)

        def _parse_window(data: object) -> CutoverWindowDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                window_type_0 = CutoverWindowDTO.from_dict(data)

                return window_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CutoverWindowDTO | None | Unset, data)

        window = _parse_window(d.pop("window", UNSET))

        evidence_errors = cast(list[str], d.pop("evidence_errors", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_cutover_window_close_response = cls(
            success=success,
            event=event,
            blocking_reasons=blocking_reasons,
            measures=measures,
            window=window,
            evidence_errors=evidence_errors,
            error=error,
        )

        return playbook_cutover_window_close_response
