from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.task_split_request_moved import TaskSplitRequestMoved


T = TypeVar("T", bound="TaskSplitRequest")


@_attrs_define
class TaskSplitRequest:
    """
    Attributes:
        task_id (str):
        retained_acceptance (list[Any]): Exact source criteria retained; together with moved.acceptance, an exhaustive
            partition.
        moved (TaskSplitRequestMoved):
        reason (str):
        expected_generation (int): Parent integration checkpoint generation; for roots or disabled integration, split
            metadata generation (initially 0).
    """

    task_id: str
    retained_acceptance: list[Any]
    moved: TaskSplitRequestMoved
    reason: str
    expected_generation: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        retained_acceptance = self.retained_acceptance

        moved = self.moved.to_dict()

        reason = self.reason

        expected_generation = self.expected_generation

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "retained_acceptance": retained_acceptance,
                "moved": moved,
                "reason": reason,
                "expected_generation": expected_generation,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_split_request_moved import TaskSplitRequestMoved

        d = dict(src_dict)
        task_id = d.pop("task_id")

        retained_acceptance = cast(list[Any], d.pop("retained_acceptance"))

        moved = TaskSplitRequestMoved.from_dict(d.pop("moved"))

        reason = d.pop("reason")

        expected_generation = d.pop("expected_generation")

        task_split_request = cls(
            task_id=task_id,
            retained_acceptance=retained_acceptance,
            moved=moved,
            reason=reason,
            expected_generation=expected_generation,
        )

        task_split_request.additional_properties = d
        return task_split_request

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
