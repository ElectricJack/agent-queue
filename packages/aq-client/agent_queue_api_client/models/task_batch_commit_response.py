from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_batch_commit_response_skipped_operations_item import TaskBatchCommitResponseSkippedOperationsItem


T = TypeVar("T", bound="TaskBatchCommitResponse")


@_attrs_define
class TaskBatchCommitResponse:
    """The ids of the tasks the commit created, in batch order.

    ``already_committed`` marks a replay: the proposal was materialised by an
    earlier call and the original receipt (including missing references) is replayed.

        Attributes:
            success (bool | Unset):  Default: True.
            task_ids (list[str] | Unset):
            already_committed (bool | Unset):  Default: False.
            missing_task_ids (list[str] | Unset):
            skipped_operations (list[TaskBatchCommitResponseSkippedOperationsItem] | Unset):
    """

    success: bool | Unset = True
    task_ids: list[str] | Unset = UNSET
    already_committed: bool | Unset = False
    missing_task_ids: list[str] | Unset = UNSET
    skipped_operations: list[TaskBatchCommitResponseSkippedOperationsItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        task_ids: list[str] | Unset = UNSET
        if not isinstance(self.task_ids, Unset):
            task_ids = self.task_ids

        already_committed = self.already_committed

        missing_task_ids: list[str] | Unset = UNSET
        if not isinstance(self.missing_task_ids, Unset):
            missing_task_ids = self.missing_task_ids

        skipped_operations: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.skipped_operations, Unset):
            skipped_operations = []
            for skipped_operations_item_data in self.skipped_operations:
                skipped_operations_item = skipped_operations_item_data.to_dict()
                skipped_operations.append(skipped_operations_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if task_ids is not UNSET:
            field_dict["task_ids"] = task_ids
        if already_committed is not UNSET:
            field_dict["already_committed"] = already_committed
        if missing_task_ids is not UNSET:
            field_dict["missing_task_ids"] = missing_task_ids
        if skipped_operations is not UNSET:
            field_dict["skipped_operations"] = skipped_operations

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_batch_commit_response_skipped_operations_item import (
            TaskBatchCommitResponseSkippedOperationsItem,
        )

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        task_ids = cast(list[str], d.pop("task_ids", UNSET))

        already_committed = d.pop("already_committed", UNSET)

        missing_task_ids = cast(list[str], d.pop("missing_task_ids", UNSET))

        _skipped_operations = d.pop("skipped_operations", UNSET)
        skipped_operations: list[TaskBatchCommitResponseSkippedOperationsItem] | Unset = UNSET
        if _skipped_operations is not UNSET:
            skipped_operations = []
            for skipped_operations_item_data in _skipped_operations:
                skipped_operations_item = TaskBatchCommitResponseSkippedOperationsItem.from_dict(
                    skipped_operations_item_data
                )

                skipped_operations.append(skipped_operations_item)

        task_batch_commit_response = cls(
            success=success,
            task_ids=task_ids,
            already_committed=already_committed,
            missing_task_ids=missing_task_ids,
            skipped_operations=skipped_operations,
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
