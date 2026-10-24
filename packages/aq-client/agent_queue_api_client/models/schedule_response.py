from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.schedule_response_schedule_type_0 import ScheduleResponseScheduleType0


T = TypeVar("T", bound="ScheduleResponse")


@_attrs_define
class ScheduleResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        schedule (None | ScheduleResponseScheduleType0 | Unset):
        error (None | str | Unset):
        error_code (None | str | Unset):
    """

    success: bool | Unset = True
    schedule: None | ScheduleResponseScheduleType0 | Unset = UNSET
    error: None | str | Unset = UNSET
    error_code: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.schedule_response_schedule_type_0 import ScheduleResponseScheduleType0

        success = self.success

        schedule: dict[str, Any] | None | Unset
        if isinstance(self.schedule, Unset):
            schedule = UNSET
        elif isinstance(self.schedule, ScheduleResponseScheduleType0):
            schedule = self.schedule.to_dict()
        else:
            schedule = self.schedule

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
        if schedule is not UNSET:
            field_dict["schedule"] = schedule
        if error is not UNSET:
            field_dict["error"] = error
        if error_code is not UNSET:
            field_dict["error_code"] = error_code

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.schedule_response_schedule_type_0 import ScheduleResponseScheduleType0

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_schedule(data: object) -> None | ScheduleResponseScheduleType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                schedule_type_0 = ScheduleResponseScheduleType0.from_dict(data)

                return schedule_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ScheduleResponseScheduleType0 | Unset, data)

        schedule = _parse_schedule(d.pop("schedule", UNSET))

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

        schedule_response = cls(
            success=success,
            schedule=schedule,
            error=error,
            error_code=error_code,
        )

        schedule_response.additional_properties = d
        return schedule_response

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
