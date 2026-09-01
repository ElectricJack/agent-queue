from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.stuck_task import StuckTask
    from ..models.stuck_tasks_thresholds import StuckTasksThresholds


T = TypeVar("T", bound="GetStuckTasksResponse")


@_attrs_define
class GetStuckTasksResponse:
    """
    Attributes:
        now_used (float):
        thresholds (StuckTasksThresholds):
        stuck (list[StuckTask] | Unset):
    """

    now_used: float
    thresholds: StuckTasksThresholds
    stuck: list[StuckTask] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        now_used = self.now_used

        thresholds = self.thresholds.to_dict()

        stuck: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.stuck, Unset):
            stuck = []
            for stuck_item_data in self.stuck:
                stuck_item = stuck_item_data.to_dict()
                stuck.append(stuck_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "now_used": now_used,
                "thresholds": thresholds,
            }
        )
        if stuck is not UNSET:
            field_dict["stuck"] = stuck

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.stuck_task import StuckTask
        from ..models.stuck_tasks_thresholds import StuckTasksThresholds

        d = dict(src_dict)
        now_used = d.pop("now_used")

        thresholds = StuckTasksThresholds.from_dict(d.pop("thresholds"))

        _stuck = d.pop("stuck", UNSET)
        stuck: list[StuckTask] | Unset = UNSET
        if _stuck is not UNSET:
            stuck = []
            for stuck_item_data in _stuck:
                stuck_item = StuckTask.from_dict(stuck_item_data)

                stuck.append(stuck_item)

        get_stuck_tasks_response = cls(
            now_used=now_used,
            thresholds=thresholds,
            stuck=stuck,
        )

        get_stuck_tasks_response.additional_properties = d
        return get_stuck_tasks_response

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
