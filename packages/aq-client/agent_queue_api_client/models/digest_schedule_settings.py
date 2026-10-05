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
        supervisor_authored (bool | Unset):  Default: False.
        cadence_minutes (int | Unset):  Default: 120.
        author_fallback_minutes (int | Unset):  Default: 10.
    """

    enabled: bool
    interval_minutes: int
    catchup_hours: int
    project_ids: list[str] | Unset = UNSET
    categories: list[str] | Unset = UNSET
    supervisor_authored: bool | Unset = False
    cadence_minutes: int | Unset = 120
    author_fallback_minutes: int | Unset = 10
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

        supervisor_authored = self.supervisor_authored

        cadence_minutes = self.cadence_minutes

        author_fallback_minutes = self.author_fallback_minutes

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
        if supervisor_authored is not UNSET:
            field_dict["supervisor_authored"] = supervisor_authored
        if cadence_minutes is not UNSET:
            field_dict["cadence_minutes"] = cadence_minutes
        if author_fallback_minutes is not UNSET:
            field_dict["author_fallback_minutes"] = author_fallback_minutes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        enabled = d.pop("enabled")

        interval_minutes = d.pop("interval_minutes")

        catchup_hours = d.pop("catchup_hours")

        project_ids = cast(list[str], d.pop("project_ids", UNSET))

        categories = cast(list[str], d.pop("categories", UNSET))

        supervisor_authored = d.pop("supervisor_authored", UNSET)

        cadence_minutes = d.pop("cadence_minutes", UNSET)

        author_fallback_minutes = d.pop("author_fallback_minutes", UNSET)

        digest_schedule_settings = cls(
            enabled=enabled,
            interval_minutes=interval_minutes,
            catchup_hours=catchup_hours,
            project_ids=project_ids,
            categories=categories,
            supervisor_authored=supervisor_authored,
            cadence_minutes=cadence_minutes,
            author_fallback_minutes=author_fallback_minutes,
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
