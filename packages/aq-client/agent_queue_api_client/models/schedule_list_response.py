from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.schedule_list_response_schedules_item import ScheduleListResponseSchedulesItem


T = TypeVar("T", bound="ScheduleListResponse")


@_attrs_define
class ScheduleListResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        schedules (list[ScheduleListResponseSchedulesItem] | Unset):
        count (int | Unset):  Default: 0.
        error (None | str | Unset):
        error_code (None | str | Unset):
    """

    success: bool | Unset = True
    schedules: list[ScheduleListResponseSchedulesItem] | Unset = UNSET
    count: int | Unset = 0
    error: None | str | Unset = UNSET
    error_code: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        schedules: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.schedules, Unset):
            schedules = []
            for schedules_item_data in self.schedules:
                schedules_item = schedules_item_data.to_dict()
                schedules.append(schedules_item)

        count = self.count

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if schedules is not UNSET:
            field_dict["schedules"] = schedules
        if count is not UNSET:
            field_dict["count"] = count
        if error is not UNSET:
            field_dict["error"] = error
        if error_code is not UNSET:
            field_dict["error_code"] = error_code

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.schedule_list_response_schedules_item import ScheduleListResponseSchedulesItem

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _schedules = d.pop("schedules", UNSET)
        schedules: list[ScheduleListResponseSchedulesItem] | Unset = UNSET
        if _schedules is not UNSET:
            schedules = []
            for schedules_item_data in _schedules:
                schedules_item = ScheduleListResponseSchedulesItem.from_dict(schedules_item_data)

                schedules.append(schedules_item)

        count = d.pop("count", UNSET)

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))

        schedule_list_response = cls(
            success=success,
            schedules=schedules,
            count=count,
            error=error,
            error_code=error_code,
        )

        schedule_list_response.additional_properties = d
        return schedule_list_response

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
