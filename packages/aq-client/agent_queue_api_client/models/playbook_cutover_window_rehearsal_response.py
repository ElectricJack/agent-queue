from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cutover_event_dto import CutoverEventDTO
    from ..models.playbook_cutover_window_rehearsal_response_errors import PlaybookCutoverWindowRehearsalResponseErrors
    from ..models.playbook_cutover_window_rehearsal_response_runs import PlaybookCutoverWindowRehearsalResponseRuns


T = TypeVar("T", bound="PlaybookCutoverWindowRehearsalResponse")


@_attrs_define
class PlaybookCutoverWindowRehearsalResponse:
    """One synthetic live dispatch per enabled playbook, recorded in the audit.

    Attributes:
        success (bool):
        event (CutoverEventDTO | None | Unset):
        playbooks (list[str] | Unset):
        runs (PlaybookCutoverWindowRehearsalResponseRuns | Unset):
        uncovered (list[str] | Unset):
        errors (PlaybookCutoverWindowRehearsalResponseErrors | Unset):
        error (None | str | Unset):
    """

    success: bool
    event: CutoverEventDTO | None | Unset = UNSET
    playbooks: list[str] | Unset = UNSET
    runs: PlaybookCutoverWindowRehearsalResponseRuns | Unset = UNSET
    uncovered: list[str] | Unset = UNSET
    errors: PlaybookCutoverWindowRehearsalResponseErrors | Unset = UNSET
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

        playbooks: list[str] | Unset = UNSET
        if not isinstance(self.playbooks, Unset):
            playbooks = self.playbooks

        runs: dict[str, Any] | Unset = UNSET
        if not isinstance(self.runs, Unset):
            runs = self.runs.to_dict()

        uncovered: list[str] | Unset = UNSET
        if not isinstance(self.uncovered, Unset):
            uncovered = self.uncovered

        errors: dict[str, Any] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = self.errors.to_dict()

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
        if playbooks is not UNSET:
            field_dict["playbooks"] = playbooks
        if runs is not UNSET:
            field_dict["runs"] = runs
        if uncovered is not UNSET:
            field_dict["uncovered"] = uncovered
        if errors is not UNSET:
            field_dict["errors"] = errors
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cutover_event_dto import CutoverEventDTO
        from ..models.playbook_cutover_window_rehearsal_response_errors import (
            PlaybookCutoverWindowRehearsalResponseErrors,
        )
        from ..models.playbook_cutover_window_rehearsal_response_runs import PlaybookCutoverWindowRehearsalResponseRuns

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

        playbooks = cast(list[str], d.pop("playbooks", UNSET))

        _runs = d.pop("runs", UNSET)
        runs: PlaybookCutoverWindowRehearsalResponseRuns | Unset
        if isinstance(_runs, Unset):
            runs = UNSET
        else:
            runs = PlaybookCutoverWindowRehearsalResponseRuns.from_dict(_runs)

        uncovered = cast(list[str], d.pop("uncovered", UNSET))

        _errors = d.pop("errors", UNSET)
        errors: PlaybookCutoverWindowRehearsalResponseErrors | Unset
        if isinstance(_errors, Unset):
            errors = UNSET
        else:
            errors = PlaybookCutoverWindowRehearsalResponseErrors.from_dict(_errors)

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_cutover_window_rehearsal_response = cls(
            success=success,
            event=event,
            playbooks=playbooks,
            runs=runs,
            uncovered=uncovered,
            errors=errors,
            error=error,
        )

        return playbook_cutover_window_rehearsal_response
