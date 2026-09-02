from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="PlaybookPendingEventActionRequest")


@_attrs_define
class PlaybookPendingEventActionRequest:
    """
    Attributes:
        action (str): What to do with the listed events.
        pending_event_ids (list[Any]): Non-empty list of pending event ids to act on.
    """

    action: str
    pending_event_ids: list[Any]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        action = self.action

        pending_event_ids = self.pending_event_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "action": action,
                "pending_event_ids": pending_event_ids,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        action = d.pop("action")

        pending_event_ids = cast(list[Any], d.pop("pending_event_ids"))

        playbook_pending_event_action_request = cls(
            action=action,
            pending_event_ids=pending_event_ids,
        )

        playbook_pending_event_action_request.additional_properties = d
        return playbook_pending_event_action_request

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
