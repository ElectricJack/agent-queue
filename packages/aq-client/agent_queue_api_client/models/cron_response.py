from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cron_response_schedule import CronResponseSchedule


T = TypeVar("T", bound="CronResponse")


@_attrs_define
class CronResponse:
    """
    Attributes:
        schedule (CronResponseSchedule):
        success (bool | Unset):  Default: True.
        next_step (None | str | Unset):
    """

    schedule: CronResponseSchedule
    success: bool | Unset = True
    next_step: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schedule = self.schedule.to_dict()

        success = self.success

        next_step: None | str | Unset
        if isinstance(self.next_step, Unset):
            next_step = UNSET
        else:
            next_step = self.next_step

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "schedule": schedule,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if next_step is not UNSET:
            field_dict["next_step"] = next_step

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cron_response_schedule import CronResponseSchedule

        d = dict(src_dict)
        schedule = CronResponseSchedule.from_dict(d.pop("schedule"))

        success = d.pop("success", UNSET)

        def _parse_next_step(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_step = _parse_next_step(d.pop("next_step", UNSET))

        cron_response = cls(
            schedule=schedule,
            success=success,
            next_step=next_step,
        )

        cron_response.additional_properties = d
        return cron_response

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
