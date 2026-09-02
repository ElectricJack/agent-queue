from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.list_playbook_pending_events_response_by_reason import ListPlaybookPendingEventsResponseByReason
    from ..models.pending_event_dto import PendingEventDTO


T = TypeVar("T", bound="ListPlaybookPendingEventsResponse")


@_attrs_define
class ListPlaybookPendingEventsResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        events (list[PendingEventDTO] | Unset):
        count (int | Unset):  Default: 0.
        oldest_received_at (float | None | Unset):
        by_reason (ListPlaybookPendingEventsResponseByReason | Unset):
    """

    success: bool | Unset = True
    events: list[PendingEventDTO] | Unset = UNSET
    count: int | Unset = 0
    oldest_received_at: float | None | Unset = UNSET
    by_reason: ListPlaybookPendingEventsResponseByReason | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        events: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.events, Unset):
            events = []
            for events_item_data in self.events:
                events_item = events_item_data.to_dict()
                events.append(events_item)

        count = self.count

        oldest_received_at: float | None | Unset
        if isinstance(self.oldest_received_at, Unset):
            oldest_received_at = UNSET
        else:
            oldest_received_at = self.oldest_received_at

        by_reason: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_reason, Unset):
            by_reason = self.by_reason.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if events is not UNSET:
            field_dict["events"] = events
        if count is not UNSET:
            field_dict["count"] = count
        if oldest_received_at is not UNSET:
            field_dict["oldest_received_at"] = oldest_received_at
        if by_reason is not UNSET:
            field_dict["by_reason"] = by_reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.list_playbook_pending_events_response_by_reason import ListPlaybookPendingEventsResponseByReason
        from ..models.pending_event_dto import PendingEventDTO

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _events = d.pop("events", UNSET)
        events: list[PendingEventDTO] | Unset = UNSET
        if _events is not UNSET:
            events = []
            for events_item_data in _events:
                events_item = PendingEventDTO.from_dict(events_item_data)

                events.append(events_item)

        count = d.pop("count", UNSET)

        def _parse_oldest_received_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        oldest_received_at = _parse_oldest_received_at(d.pop("oldest_received_at", UNSET))

        _by_reason = d.pop("by_reason", UNSET)
        by_reason: ListPlaybookPendingEventsResponseByReason | Unset
        if isinstance(_by_reason, Unset):
            by_reason = UNSET
        else:
            by_reason = ListPlaybookPendingEventsResponseByReason.from_dict(_by_reason)

        list_playbook_pending_events_response = cls(
            success=success,
            events=events,
            count=count,
            oldest_received_at=oldest_received_at,
            by_reason=by_reason,
        )

        return list_playbook_pending_events_response
