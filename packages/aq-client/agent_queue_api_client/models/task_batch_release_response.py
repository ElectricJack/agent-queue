from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_batch_release_response_release_receipt import TaskBatchReleaseResponseReleaseReceipt


T = TypeVar("T", bound="TaskBatchReleaseResponse")


@_attrs_define
class TaskBatchReleaseResponse:
    """The original atomic release receipt is returned on every successful replay.

    Attributes:
        batch_id (str):
        already_released (bool):
        release_receipt (TaskBatchReleaseResponseReleaseReceipt):
        success (bool | Unset):  Default: True.
    """

    batch_id: str
    already_released: bool
    release_receipt: TaskBatchReleaseResponseReleaseReceipt
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        batch_id = self.batch_id

        already_released = self.already_released

        release_receipt = self.release_receipt.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "batch_id": batch_id,
                "already_released": already_released,
                "release_receipt": release_receipt,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_batch_release_response_release_receipt import TaskBatchReleaseResponseReleaseReceipt

        d = dict(src_dict)
        batch_id = d.pop("batch_id")

        already_released = d.pop("already_released")

        release_receipt = TaskBatchReleaseResponseReleaseReceipt.from_dict(d.pop("release_receipt"))

        success = d.pop("success", UNSET)

        task_batch_release_response = cls(
            batch_id=batch_id,
            already_released=already_released,
            release_receipt=release_receipt,
            success=success,
        )

        task_batch_release_response.additional_properties = d
        return task_batch_release_response

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
