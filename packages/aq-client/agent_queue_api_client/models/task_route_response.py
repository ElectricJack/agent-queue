from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskRouteResponse")


@_attrs_define
class TaskRouteResponse:
    """
    Attributes:
        task_id (str):
        success (bool | Unset):  Default: True.
        resolved_gate_ids (list[str] | Unset):
    """

    task_id: str
    success: bool | Unset = True
    resolved_gate_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        success = self.success

        resolved_gate_ids: list[str] | Unset = UNSET
        if not isinstance(self.resolved_gate_ids, Unset):
            resolved_gate_ids = self.resolved_gate_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if resolved_gate_ids is not UNSET:
            field_dict["resolved_gate_ids"] = resolved_gate_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        success = d.pop("success", UNSET)

        resolved_gate_ids = cast(list[str], d.pop("resolved_gate_ids", UNSET))

        task_route_response = cls(
            task_id=task_id,
            success=success,
            resolved_gate_ids=resolved_gate_ids,
        )

        task_route_response.additional_properties = d
        return task_route_response

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
