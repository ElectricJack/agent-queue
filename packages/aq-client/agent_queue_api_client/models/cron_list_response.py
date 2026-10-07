from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cron_list_response_schedules_item import CronListResponseSchedulesItem


T = TypeVar("T", bound="CronListResponse")


@_attrs_define
class CronListResponse:
    """
    Attributes:
        schedules (list[CronListResponseSchedulesItem]):
        count (int):
        success (bool | Unset):  Default: True.
    """

    schedules: list[CronListResponseSchedulesItem]
    count: int
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schedules = []
        for schedules_item_data in self.schedules:
            schedules_item = schedules_item_data.to_dict()
            schedules.append(schedules_item)

        count = self.count

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "schedules": schedules,
                "count": count,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cron_list_response_schedules_item import CronListResponseSchedulesItem

        d = dict(src_dict)
        schedules = []
        _schedules = d.pop("schedules")
        for schedules_item_data in _schedules:
            schedules_item = CronListResponseSchedulesItem.from_dict(schedules_item_data)

            schedules.append(schedules_item)

        count = d.pop("count")

        success = d.pop("success", UNSET)

        cron_list_response = cls(
            schedules=schedules,
            count=count,
            success=success,
        )

        cron_list_response.additional_properties = d
        return cron_list_response

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
