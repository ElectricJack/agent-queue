from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.remove_task_response_disposition import RemoveTaskResponseDisposition
from ..types import UNSET, Unset

T = TypeVar("T", bound="RemoveTaskResponse")


@_attrs_define
class RemoveTaskResponse:
    """
    Attributes:
        title (str):
        disposition (RemoveTaskResponseDisposition):
        success (bool | Unset):  Default: True.
        removed (None | str | Unset):
        task_ids (list[str] | Unset):
        branches (Literal['keep'] | Unset):  Default: 'keep'.
        aborted_batches (list[str] | Unset):
        cancelled_operations (list[str] | Unset):
    """

    title: str
    disposition: RemoveTaskResponseDisposition
    success: bool | Unset = True
    removed: None | str | Unset = UNSET
    task_ids: list[str] | Unset = UNSET
    branches: Literal["keep"] | Unset = "keep"
    aborted_batches: list[str] | Unset = UNSET
    cancelled_operations: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        title = self.title

        disposition = self.disposition.value

        success = self.success

        removed: None | str | Unset
        if isinstance(self.removed, Unset):
            removed = UNSET
        else:
            removed = self.removed

        task_ids: list[str] | Unset = UNSET
        if not isinstance(self.task_ids, Unset):
            task_ids = self.task_ids

        branches = self.branches

        aborted_batches: list[str] | Unset = UNSET
        if not isinstance(self.aborted_batches, Unset):
            aborted_batches = self.aborted_batches

        cancelled_operations: list[str] | Unset = UNSET
        if not isinstance(self.cancelled_operations, Unset):
            cancelled_operations = self.cancelled_operations

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "title": title,
                "disposition": disposition,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if removed is not UNSET:
            field_dict["removed"] = removed
        if task_ids is not UNSET:
            field_dict["task_ids"] = task_ids
        if branches is not UNSET:
            field_dict["branches"] = branches
        if aborted_batches is not UNSET:
            field_dict["aborted_batches"] = aborted_batches
        if cancelled_operations is not UNSET:
            field_dict["cancelled_operations"] = cancelled_operations

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        title = d.pop("title")

        disposition = RemoveTaskResponseDisposition(d.pop("disposition"))

        success = d.pop("success", UNSET)

        def _parse_removed(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        removed = _parse_removed(d.pop("removed", UNSET))

        task_ids = cast(list[str], d.pop("task_ids", UNSET))

        branches = cast(Literal["keep"] | Unset, d.pop("branches", UNSET))
        if branches != "keep" and not isinstance(branches, Unset):
            raise ValueError(f"branches must match const 'keep', got '{branches}'")

        aborted_batches = cast(list[str], d.pop("aborted_batches", UNSET))

        cancelled_operations = cast(list[str], d.pop("cancelled_operations", UNSET))

        remove_task_response = cls(
            title=title,
            disposition=disposition,
            success=success,
            removed=removed,
            task_ids=task_ids,
            branches=branches,
            aborted_batches=aborted_batches,
            cancelled_operations=cancelled_operations,
        )

        remove_task_response.additional_properties = d
        return remove_task_response

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
