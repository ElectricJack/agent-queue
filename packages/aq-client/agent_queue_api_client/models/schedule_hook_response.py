from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ScheduleHookResponse")


@_attrs_define
class ScheduleHookResponse:
    """
    Attributes:
        created (str):
        name (str | Unset):  Default: ''.
        project_id (str | Unset):  Default: ''.
        fire_at (str | Unset):  Default: ''.
        fire_at_epoch (float | Unset):  Default: 0.0.
        fires_in (str | Unset):  Default: ''.
    """

    created: str
    name: str | Unset = ""
    project_id: str | Unset = ""
    fire_at: str | Unset = ""
    fire_at_epoch: float | Unset = 0.0
    fires_in: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        name = self.name

        project_id = self.project_id

        fire_at = self.fire_at

        fire_at_epoch = self.fire_at_epoch

        fires_in = self.fires_in

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created": created,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if fire_at is not UNSET:
            field_dict["fire_at"] = fire_at
        if fire_at_epoch is not UNSET:
            field_dict["fire_at_epoch"] = fire_at_epoch
        if fires_in is not UNSET:
            field_dict["fires_in"] = fires_in

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        created = d.pop("created")

        name = d.pop("name", UNSET)

        project_id = d.pop("project_id", UNSET)

        fire_at = d.pop("fire_at", UNSET)

        fire_at_epoch = d.pop("fire_at_epoch", UNSET)

        fires_in = d.pop("fires_in", UNSET)

        schedule_hook_response = cls(
            created=created,
            name=name,
            project_id=project_id,
            fire_at=fire_at,
            fire_at_epoch=fire_at_epoch,
            fires_in=fires_in,
        )

        schedule_hook_response.additional_properties = d
        return schedule_hook_response

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
