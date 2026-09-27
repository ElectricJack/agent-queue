from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskDeliverRequest")


@_attrs_define
class TaskDeliverRequest:
    """
    Attributes:
        task_id (str): The BLOCKED task whose recorded branch is delivered.
        reason (str): Audit reason; recorded on the task and in the merge commit.
        dry_run (bool | Unset): Report the delivery plan without pushing or completing. Default: False.
    """

    task_id: str
    reason: str
    dry_run: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        reason = self.reason

        dry_run = self.dry_run

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "reason": reason,
            }
        )
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        reason = d.pop("reason")

        dry_run = d.pop("dry_run", UNSET)

        task_deliver_request = cls(
            task_id=task_id,
            reason=reason,
            dry_run=dry_run,
        )

        task_deliver_request.additional_properties = d
        return task_deliver_request

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
