from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_source_record import AskSourceRecord


T = TypeVar("T", bound="GateReopenResponse")


@_attrs_define
class GateReopenResponse:
    """
    Attributes:
        gate_id (str):
        ask (AskSourceRecord):
        success (bool | Unset):  Default: True.
    """

    gate_id: str
    ask: AskSourceRecord
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        gate_id = self.gate_id

        ask = self.ask.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "gate_id": gate_id,
                "ask": ask,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_source_record import AskSourceRecord

        d = dict(src_dict)
        gate_id = d.pop("gate_id")

        ask = AskSourceRecord.from_dict(d.pop("ask"))

        success = d.pop("success", UNSET)

        gate_reopen_response = cls(
            gate_id=gate_id,
            ask=ask,
            success=success,
        )

        gate_reopen_response.additional_properties = d
        return gate_reopen_response

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
