from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="SchedulePointerArgs")


@_attrs_define
class SchedulePointerArgs:
    """
    Attributes:
        project_id (str):
        schedule_id (str):
    """

    project_id: str
    schedule_id: str

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        schedule_id = self.schedule_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "project_id": project_id,
                "schedule_id": schedule_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        schedule_id = d.pop("schedule_id")

        schedule_pointer_args = cls(
            project_id=project_id,
            schedule_id=schedule_id,
        )

        return schedule_pointer_args
