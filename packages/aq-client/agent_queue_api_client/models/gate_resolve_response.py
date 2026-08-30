from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GateResolveResponse")


@_attrs_define
class GateResolveResponse:
    """
    Attributes:
        gate_id (str):
        success (bool | Unset):  Default: True.
        unblocked_task_ids (list[str] | Unset):
    """

    gate_id: str
    success: bool | Unset = True
    unblocked_task_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        gate_id = self.gate_id

        success = self.success

        unblocked_task_ids: list[str] | Unset = UNSET
        if not isinstance(self.unblocked_task_ids, Unset):
            unblocked_task_ids = self.unblocked_task_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "gate_id": gate_id,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if unblocked_task_ids is not UNSET:
            field_dict["unblocked_task_ids"] = unblocked_task_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        gate_id = d.pop("gate_id")

        success = d.pop("success", UNSET)

        unblocked_task_ids = cast(list[str], d.pop("unblocked_task_ids", UNSET))

        gate_resolve_response = cls(
            gate_id=gate_id,
            success=success,
            unblocked_task_ids=unblocked_task_ids,
        )

        gate_resolve_response.additional_properties = d
        return gate_resolve_response

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
