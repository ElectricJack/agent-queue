from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskBatchRetireResponse")


@_attrs_define
class TaskBatchRetireResponse:
    """
    Attributes:
        batch_id (str):
        state (Literal['retired']):
        already_retired (bool):
        success (bool | Unset):  Default: True.
    """

    batch_id: str
    state: Literal["retired"]
    already_retired: bool
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        batch_id = self.batch_id

        state = self.state

        already_retired = self.already_retired

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "batch_id": batch_id,
                "state": state,
                "already_retired": already_retired,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        batch_id = d.pop("batch_id")

        state = cast(Literal["retired"], d.pop("state"))
        if state != "retired":
            raise ValueError(f"state must match const 'retired', got '{state}'")

        already_retired = d.pop("already_retired")

        success = d.pop("success", UNSET)

        task_batch_retire_response = cls(
            batch_id=batch_id,
            state=state,
            already_retired=already_retired,
            success=success,
        )

        task_batch_retire_response.additional_properties = d
        return task_batch_retire_response

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
