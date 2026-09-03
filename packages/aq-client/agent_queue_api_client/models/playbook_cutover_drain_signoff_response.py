from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cutover_event_dto import CutoverEventDTO
    from ..models.playbook_cutover_drain_signoff_response_checks_item import (
        PlaybookCutoverDrainSignoffResponseChecksItem,
    )


T = TypeVar("T", bound="PlaybookCutoverDrainSignoffResponse")


@_attrs_define
class PlaybookCutoverDrainSignoffResponse:
    """
    Attributes:
        success (bool):
        event (CutoverEventDTO | None | Unset):
        checks (list[PlaybookCutoverDrainSignoffResponseChecksItem] | Unset):
        blocking_reasons (list[str] | Unset):
        error (None | str | Unset):
    """

    success: bool
    event: CutoverEventDTO | None | Unset = UNSET
    checks: list[PlaybookCutoverDrainSignoffResponseChecksItem] | Unset = UNSET
    blocking_reasons: list[str] | Unset = UNSET
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

        checks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.checks, Unset):
            checks = []
            for checks_item_data in self.checks:
                checks_item = checks_item_data.to_dict()
                checks.append(checks_item)

        blocking_reasons: list[str] | Unset = UNSET
        if not isinstance(self.blocking_reasons, Unset):
            blocking_reasons = self.blocking_reasons

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
        if checks is not UNSET:
            field_dict["checks"] = checks
        if blocking_reasons is not UNSET:
            field_dict["blocking_reasons"] = blocking_reasons
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cutover_event_dto import CutoverEventDTO
        from ..models.playbook_cutover_drain_signoff_response_checks_item import (
            PlaybookCutoverDrainSignoffResponseChecksItem,
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

        _checks = d.pop("checks", UNSET)
        checks: list[PlaybookCutoverDrainSignoffResponseChecksItem] | Unset = UNSET
        if _checks is not UNSET:
            checks = []
            for checks_item_data in _checks:
                checks_item = PlaybookCutoverDrainSignoffResponseChecksItem.from_dict(checks_item_data)

                checks.append(checks_item)

        blocking_reasons = cast(list[str], d.pop("blocking_reasons", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_cutover_drain_signoff_response = cls(
            success=success,
            event=event,
            checks=checks,
            blocking_reasons=blocking_reasons,
            error=error,
        )

        return playbook_cutover_drain_signoff_response
