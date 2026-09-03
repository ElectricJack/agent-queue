from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookPendingEventActionRequest")


@_attrs_define
class PlaybookPendingEventActionRequest:
    """
    Attributes:
        action (str): What to do with the listed events.
        pending_event_ids (list[Any]): Non-empty list of pending event ids to act on.
        reason (None | str | Unset): Required for 'discard': why these events may be dropped, at least 12 characters.
            Recorded on every row.
    """

    action: str
    pending_event_ids: list[Any]
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        action = self.action

        pending_event_ids = self.pending_event_ids

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "action": action,
                "pending_event_ids": pending_event_ids,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        action = d.pop("action")

        pending_event_ids = cast(list[Any], d.pop("pending_event_ids"))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        playbook_pending_event_action_request = cls(
            action=action,
            pending_event_ids=pending_event_ids,
            reason=reason,
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
