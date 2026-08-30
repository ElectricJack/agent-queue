from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GraphGate")


@_attrs_define
class GraphGate:
    """
    Attributes:
        id (str):
        gate_type (str):
        status (str):
        task_ids (list[str] | Unset):
    """

    id: str
    gate_type: str
    status: str
    task_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        gate_type = self.gate_type

        status = self.status

        task_ids: list[str] | Unset = UNSET
        if not isinstance(self.task_ids, Unset):
            task_ids = self.task_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "gate_type": gate_type,
                "status": status,
            }
        )
        if task_ids is not UNSET:
            field_dict["task_ids"] = task_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        gate_type = d.pop("gate_type")

        status = d.pop("status")

        task_ids = cast(list[str], d.pop("task_ids", UNSET))

        graph_gate = cls(
            id=id,
            gate_type=gate_type,
            status=status,
            task_ids=task_ids,
        )

        graph_gate.additional_properties = d
        return graph_gate

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
