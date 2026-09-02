from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.pending_event_dto_reason import PendingEventDTOReason
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.pending_event_dto_event import PendingEventDTOEvent


T = TypeVar("T", bound="PendingEventDTO")


@_attrs_define
class PendingEventDTO:
    """
    Attributes:
        pending_event_id (str):
        playbook_id (str):
        event_type (str):
        received_at (float):
        reason (PendingEventDTOReason):
        event (PendingEventDTOEvent | Unset):
        attempts (int | Unset):  Default: 0.
        last_error (None | str | Unset):
        expires_at (float | None | Unset):
    """

    pending_event_id: str
    playbook_id: str
    event_type: str
    received_at: float
    reason: PendingEventDTOReason
    event: PendingEventDTOEvent | Unset = UNSET
    attempts: int | Unset = 0
    last_error: None | str | Unset = UNSET
    expires_at: float | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        pending_event_id = self.pending_event_id

        playbook_id = self.playbook_id

        event_type = self.event_type

        received_at = self.received_at

        reason = self.reason.value

        event: dict[str, Any] | Unset = UNSET
        if not isinstance(self.event, Unset):
            event = self.event.to_dict()

        attempts = self.attempts

        last_error: None | str | Unset
        if isinstance(self.last_error, Unset):
            last_error = UNSET
        else:
            last_error = self.last_error

        expires_at: float | None | Unset
        if isinstance(self.expires_at, Unset):
            expires_at = UNSET
        else:
            expires_at = self.expires_at

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "pending_event_id": pending_event_id,
                "playbook_id": playbook_id,
                "event_type": event_type,
                "received_at": received_at,
                "reason": reason,
            }
        )
        if event is not UNSET:
            field_dict["event"] = event
        if attempts is not UNSET:
            field_dict["attempts"] = attempts
        if last_error is not UNSET:
            field_dict["last_error"] = last_error
        if expires_at is not UNSET:
            field_dict["expires_at"] = expires_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.pending_event_dto_event import PendingEventDTOEvent

        d = dict(src_dict)
        pending_event_id = d.pop("pending_event_id")

        playbook_id = d.pop("playbook_id")

        event_type = d.pop("event_type")

        received_at = d.pop("received_at")

        reason = PendingEventDTOReason(d.pop("reason"))

        _event = d.pop("event", UNSET)
        event: PendingEventDTOEvent | Unset
        if isinstance(_event, Unset):
            event = UNSET
        else:
            event = PendingEventDTOEvent.from_dict(_event)

        attempts = d.pop("attempts", UNSET)

        def _parse_last_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        last_error = _parse_last_error(d.pop("last_error", UNSET))

        def _parse_expires_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        expires_at = _parse_expires_at(d.pop("expires_at", UNSET))

        pending_event_dto = cls(
            pending_event_id=pending_event_id,
            playbook_id=playbook_id,
            event_type=event_type,
            received_at=received_at,
            reason=reason,
            event=event,
            attempts=attempts,
            last_error=last_error,
            expires_at=expires_at,
        )

        return pending_event_dto
