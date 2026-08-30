from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="StuckTasksThresholds")


@_attrs_define
class StuckTasksThresholds:
    """
    Attributes:
        assigned (int):
        in_progress (int):
    """

    assigned: int
    in_progress: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        assigned = self.assigned

        in_progress = self.in_progress

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "assigned": assigned,
                "in_progress": in_progress,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        assigned = d.pop("assigned")

        in_progress = d.pop("in_progress")

        stuck_tasks_thresholds = cls(
            assigned=assigned,
            in_progress=in_progress,
        )

        stuck_tasks_thresholds.additional_properties = d
        return stuck_tasks_thresholds

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
