from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.gate_reopen_request_expected import GateReopenRequestExpected


T = TypeVar("T", bound="GateReopenRequest")


@_attrs_define
class GateReopenRequest:
    """
    Attributes:
        gate_id (str):
        expected (GateReopenRequestExpected):
    """

    gate_id: str
    expected: GateReopenRequestExpected
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        gate_id = self.gate_id

        expected = self.expected.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "gate_id": gate_id,
                "expected": expected,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.gate_reopen_request_expected import GateReopenRequestExpected

        d = dict(src_dict)
        gate_id = d.pop("gate_id")

        expected = GateReopenRequestExpected.from_dict(d.pop("expected"))

        gate_reopen_request = cls(
            gate_id=gate_id,
            expected=expected,
        )

        gate_reopen_request.additional_properties = d
        return gate_reopen_request

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
