from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.archive_settings_response_blocked_item import ArchiveSettingsResponseBlockedItem


T = TypeVar("T", bound="ArchiveSettingsResponse")


@_attrs_define
class ArchiveSettingsResponse:
    """
    Attributes:
        enabled (bool | Unset):  Default: False.
        after_hours (int | Unset):  Default: 0.
        statuses (list[str] | Unset):
        archived_count (int | Unset):  Default: 0.
        eligible_count (int | Unset):  Default: 0.
        blocked_count (int | Unset):  Default: 0.
        blocked (list[ArchiveSettingsResponseBlockedItem] | Unset):
    """

    enabled: bool | Unset = False
    after_hours: int | Unset = 0
    statuses: list[str] | Unset = UNSET
    archived_count: int | Unset = 0
    eligible_count: int | Unset = 0
    blocked_count: int | Unset = 0
    blocked: list[ArchiveSettingsResponseBlockedItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        enabled = self.enabled

        after_hours = self.after_hours

        statuses: list[str] | Unset = UNSET
        if not isinstance(self.statuses, Unset):
            statuses = self.statuses

        archived_count = self.archived_count

        eligible_count = self.eligible_count

        blocked_count = self.blocked_count

        blocked: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.blocked, Unset):
            blocked = []
            for blocked_item_data in self.blocked:
                blocked_item = blocked_item_data.to_dict()
                blocked.append(blocked_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if after_hours is not UNSET:
            field_dict["after_hours"] = after_hours
        if statuses is not UNSET:
            field_dict["statuses"] = statuses
        if archived_count is not UNSET:
            field_dict["archived_count"] = archived_count
        if eligible_count is not UNSET:
            field_dict["eligible_count"] = eligible_count
        if blocked_count is not UNSET:
            field_dict["blocked_count"] = blocked_count
        if blocked is not UNSET:
            field_dict["blocked"] = blocked

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.archive_settings_response_blocked_item import ArchiveSettingsResponseBlockedItem

        d = dict(src_dict)
        enabled = d.pop("enabled", UNSET)

        after_hours = d.pop("after_hours", UNSET)

        statuses = cast(list[str], d.pop("statuses", UNSET))

        archived_count = d.pop("archived_count", UNSET)

        eligible_count = d.pop("eligible_count", UNSET)

        blocked_count = d.pop("blocked_count", UNSET)

        _blocked = d.pop("blocked", UNSET)
        blocked: list[ArchiveSettingsResponseBlockedItem] | Unset = UNSET
        if _blocked is not UNSET:
            blocked = []
            for blocked_item_data in _blocked:
                blocked_item = ArchiveSettingsResponseBlockedItem.from_dict(blocked_item_data)

                blocked.append(blocked_item)

        archive_settings_response = cls(
            enabled=enabled,
            after_hours=after_hours,
            statuses=statuses,
            archived_count=archived_count,
            eligible_count=eligible_count,
            blocked_count=blocked_count,
            blocked=blocked,
        )

        archive_settings_response.additional_properties = d
        return archive_settings_response

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
