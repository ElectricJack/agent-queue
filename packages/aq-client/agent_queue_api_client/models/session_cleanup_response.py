from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.session_cleanup_response_pruned_item import SessionCleanupResponsePrunedItem
    from ..models.session_cleanup_response_sessions_item import SessionCleanupResponseSessionsItem
    from ..models.session_cleanup_response_skipped_item import SessionCleanupResponseSkippedItem


T = TypeVar("T", bound="SessionCleanupResponse")


@_attrs_define
class SessionCleanupResponse:
    """``session_cleanup`` — taskless sleeping named sessions stopped and pruned.

    With ``dry_run`` only ``count`` and ``sessions`` are filled.

        Attributes:
            success (bool | Unset):  Default: True.
            dry_run (bool | Unset):  Default: False.
            count (int | Unset):  Default: 0.
            sessions (list[SessionCleanupResponseSessionsItem] | Unset):
            pruned (list[SessionCleanupResponsePrunedItem] | Unset):
            skipped (list[SessionCleanupResponseSkippedItem] | Unset):
    """

    success: bool | Unset = True
    dry_run: bool | Unset = False
    count: int | Unset = 0
    sessions: list[SessionCleanupResponseSessionsItem] | Unset = UNSET
    pruned: list[SessionCleanupResponsePrunedItem] | Unset = UNSET
    skipped: list[SessionCleanupResponseSkippedItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        dry_run = self.dry_run

        count = self.count

        sessions: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.sessions, Unset):
            sessions = []
            for sessions_item_data in self.sessions:
                sessions_item = sessions_item_data.to_dict()
                sessions.append(sessions_item)

        pruned: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.pruned, Unset):
            pruned = []
            for pruned_item_data in self.pruned:
                pruned_item = pruned_item_data.to_dict()
                pruned.append(pruned_item)

        skipped: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.skipped, Unset):
            skipped = []
            for skipped_item_data in self.skipped:
                skipped_item = skipped_item_data.to_dict()
                skipped.append(skipped_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if count is not UNSET:
            field_dict["count"] = count
        if sessions is not UNSET:
            field_dict["sessions"] = sessions
        if pruned is not UNSET:
            field_dict["pruned"] = pruned
        if skipped is not UNSET:
            field_dict["skipped"] = skipped

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.session_cleanup_response_pruned_item import SessionCleanupResponsePrunedItem
        from ..models.session_cleanup_response_sessions_item import SessionCleanupResponseSessionsItem
        from ..models.session_cleanup_response_skipped_item import SessionCleanupResponseSkippedItem

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        dry_run = d.pop("dry_run", UNSET)

        count = d.pop("count", UNSET)

        _sessions = d.pop("sessions", UNSET)
        sessions: list[SessionCleanupResponseSessionsItem] | Unset = UNSET
        if _sessions is not UNSET:
            sessions = []
            for sessions_item_data in _sessions:
                sessions_item = SessionCleanupResponseSessionsItem.from_dict(sessions_item_data)

                sessions.append(sessions_item)

        _pruned = d.pop("pruned", UNSET)
        pruned: list[SessionCleanupResponsePrunedItem] | Unset = UNSET
        if _pruned is not UNSET:
            pruned = []
            for pruned_item_data in _pruned:
                pruned_item = SessionCleanupResponsePrunedItem.from_dict(pruned_item_data)

                pruned.append(pruned_item)

        _skipped = d.pop("skipped", UNSET)
        skipped: list[SessionCleanupResponseSkippedItem] | Unset = UNSET
        if _skipped is not UNSET:
            skipped = []
            for skipped_item_data in _skipped:
                skipped_item = SessionCleanupResponseSkippedItem.from_dict(skipped_item_data)

                skipped.append(skipped_item)

        session_cleanup_response = cls(
            success=success,
            dry_run=dry_run,
            count=count,
            sessions=sessions,
            pruned=pruned,
            skipped=skipped,
        )

        session_cleanup_response.additional_properties = d
        return session_cleanup_response

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
