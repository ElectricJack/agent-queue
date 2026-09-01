from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.event_trigger import EventTrigger


T = TypeVar("T", bound="ListEventTriggersResponse")


@_attrs_define
class ListEventTriggersResponse:
    """
    Attributes:
        events (list[EventTrigger] | Unset):
        count (int | Unset):  Default: 0.
    """

    events: list[EventTrigger] | Unset = UNSET
    count: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        events: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.events, Unset):
            events = []
            for events_item_data in self.events:
                events_item = events_item_data.to_dict()
                events.append(events_item)

        count = self.count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if events is not UNSET:
            field_dict["events"] = events
        if count is not UNSET:
            field_dict["count"] = count

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.event_trigger import EventTrigger  # noqa: PLC0415

        d = dict(src_dict)
        _events = d.pop("events", UNSET)
        events: list[EventTrigger] | Unset = UNSET
        if _events is not UNSET:
            events = []
            for events_item_data in _events:
                events_item = EventTrigger.from_dict(events_item_data)

                events.append(events_item)

        count = d.pop("count", UNSET)

        list_event_triggers_response = cls(
            events=events,
            count=count,
        )

        list_event_triggers_response.additional_properties = d
        return list_event_triggers_response

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
