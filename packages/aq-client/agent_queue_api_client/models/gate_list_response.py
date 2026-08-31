from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.gate_summary import GateSummary


T = TypeVar("T", bound="GateListResponse")


@_attrs_define
class GateListResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        gates (list[GateSummary] | Unset):
    """

    success: bool | Unset = True
    gates: list[GateSummary] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        gates: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.gates, Unset):
            gates = []
            for gates_item_data in self.gates:
                gates_item = gates_item_data.to_dict()
                gates.append(gates_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if gates is not UNSET:
            field_dict["gates"] = gates

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.gate_summary import GateSummary  # noqa: PLC0415

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _gates = d.pop("gates", UNSET)
        gates: list[GateSummary] | Unset = UNSET
        if _gates is not UNSET:
            gates = []
            for gates_item_data in _gates:
                gates_item = GateSummary.from_dict(gates_item_data)

                gates.append(gates_item)

        gate_list_response = cls(
            success=success,
            gates=gates,
        )

        gate_list_response.additional_properties = d
        return gate_list_response

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
