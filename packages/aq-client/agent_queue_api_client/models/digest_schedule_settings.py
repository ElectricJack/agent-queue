from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DigestScheduleSettings")


@_attrs_define
class DigestScheduleSettings:
    """
    Attributes:
        enabled (bool):
        interval_minutes (int):
        catchup_hours (int):
        project_ids (list[str] | Unset):
        categories (list[str] | Unset):
    """

    enabled: bool
    interval_minutes: int
    catchup_hours: int
    project_ids: list[str] | Unset = UNSET
    categories: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        enabled = self.enabled

        interval_minutes = self.interval_minutes

        catchup_hours = self.catchup_hours

        project_ids: list[str] | Unset = UNSET
        if not isinstance(self.project_ids, Unset):
            project_ids = self.project_ids

        categories: list[str] | Unset = UNSET
        if not isinstance(self.categories, Unset):
            categories = self.categories

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "enabled": enabled,
                "interval_minutes": interval_minutes,
                "catchup_hours": catchup_hours,
            }
        )
        if project_ids is not UNSET:
            field_dict["project_ids"] = project_ids
        if categories is not UNSET:
            field_dict["categories"] = categories

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        enabled = d.pop("enabled")

        interval_minutes = d.pop("interval_minutes")

        catchup_hours = d.pop("catchup_hours")

        project_ids = cast(list[str], d.pop("project_ids", UNSET))

        categories = cast(list[str], d.pop("categories", UNSET))

        digest_schedule_settings = cls(
            enabled=enabled,
            interval_minutes=interval_minutes,
            catchup_hours=catchup_hours,
            project_ids=project_ids,
            categories=categories,
        )

        digest_schedule_settings.additional_properties = d
        return digest_schedule_settings

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
