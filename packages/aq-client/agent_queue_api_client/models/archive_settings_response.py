from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

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
    """

    enabled: bool | Unset = False
    after_hours: int | Unset = 0
    statuses: list[str] | Unset = UNSET
    archived_count: int | Unset = 0
    eligible_count: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        enabled = self.enabled

        after_hours = self.after_hours

        statuses: list[str] | Unset = UNSET
        if not isinstance(self.statuses, Unset):
            statuses = self.statuses

        archived_count = self.archived_count

        eligible_count = self.eligible_count

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

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        enabled = d.pop("enabled", UNSET)

        after_hours = d.pop("after_hours", UNSET)

        statuses = cast(list[str], d.pop("statuses", UNSET))

        archived_count = d.pop("archived_count", UNSET)

        eligible_count = d.pop("eligible_count", UNSET)

        archive_settings_response = cls(
            enabled=enabled,
            after_hours=after_hours,
            statuses=statuses,
            archived_count=archived_count,
            eligible_count=eligible_count,
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
