from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookPendingEventsRequest")


@_attrs_define
class PlaybookPendingEventsRequest:
    """
    Attributes:
        playbook_id (None | str | Unset): Restrict to one playbook. All playbooks when absent.
        reason (None | str | Unset): Filter by the reason the event is held.
        limit (int | Unset): Max events to return (default 100). Default: 100.
    """

    playbook_id: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    limit: int | Unset = 100
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id: None | str | Unset
        if isinstance(self.playbook_id, Unset):
            playbook_id = UNSET
        else:
            playbook_id = self.playbook_id

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if playbook_id is not UNSET:
            field_dict["playbook_id"] = playbook_id
        if reason is not UNSET:
            field_dict["reason"] = reason
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_playbook_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        playbook_id = _parse_playbook_id(d.pop("playbook_id", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        limit = d.pop("limit", UNSET)

        playbook_pending_events_request = cls(
            playbook_id=playbook_id,
            reason=reason,
            limit=limit,
        )

        playbook_pending_events_request.additional_properties = d
        return playbook_pending_events_request

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
