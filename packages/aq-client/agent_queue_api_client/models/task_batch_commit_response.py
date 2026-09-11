from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskBatchCommitResponse")


@_attrs_define
class TaskBatchCommitResponse:
    """The ids of the tasks the commit created, in batch order.

    ``already_committed`` marks a replay: the proposal was materialised by an
    earlier call and ``task_ids`` is that commit's receipt.

        Attributes:
            success (bool | Unset):  Default: True.
            task_ids (list[str] | Unset):
            already_committed (bool | Unset):  Default: False.
    """

    success: bool | Unset = True
    task_ids: list[str] | Unset = UNSET
    already_committed: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        task_ids: list[str] | Unset = UNSET
        if not isinstance(self.task_ids, Unset):
            task_ids = self.task_ids

        already_committed = self.already_committed

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if task_ids is not UNSET:
            field_dict["task_ids"] = task_ids
        if already_committed is not UNSET:
            field_dict["already_committed"] = already_committed

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success", UNSET)

        task_ids = cast(list[str], d.pop("task_ids", UNSET))

        already_committed = d.pop("already_committed", UNSET)

        task_batch_commit_response = cls(
            success=success,
            task_ids=task_ids,
            already_committed=already_committed,
        )

        task_batch_commit_response.additional_properties = d
        return task_batch_commit_response

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
