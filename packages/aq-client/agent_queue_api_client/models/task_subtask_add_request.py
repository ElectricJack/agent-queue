from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskSubtaskAddRequest")


@_attrs_define
class TaskSubtaskAddRequest:
    """
    Attributes:
        subtasks (list[Any]): 1 to 50 subtasks to append after the current max ordinal.
        task_id (None | str | Unset): Task id to add subtasks to. Omit to use the session's held task.
        claim_epoch (int | None | Unset): Current claim epoch; required for pool workers.
    """

    subtasks: list[Any]
    task_id: None | str | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        subtasks = self.subtasks

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "subtasks": subtasks,
            }
        )
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        subtasks = cast(list[Any], d.pop("subtasks"))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        task_subtask_add_request = cls(
            subtasks=subtasks,
            task_id=task_id,
            claim_epoch=claim_epoch,
        )

        task_subtask_add_request.additional_properties = d
        return task_subtask_add_request

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
