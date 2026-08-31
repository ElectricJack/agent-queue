from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.gate_create_payload import GateCreatePayload


T = TypeVar("T", bound="GateCreateResponse")


@_attrs_define
class GateCreateResponse:
    """
    Attributes:
        gate_id (str):
        gate (GateCreatePayload): Echoed by ``gate_create`` — matches the ``gate.created`` event payload.
        success (bool | Unset):  Default: True.
        was_created (bool | Unset):  Default: True.
    """

    gate_id: str
    gate: GateCreatePayload
    success: bool | Unset = True
    was_created: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        gate_id = self.gate_id

        gate = self.gate.to_dict()

        success = self.success

        was_created = self.was_created

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "gate_id": gate_id,
                "gate": gate,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if was_created is not UNSET:
            field_dict["was_created"] = was_created

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.gate_create_payload import GateCreatePayload  # noqa: PLC0415

        d = dict(src_dict)
        gate_id = d.pop("gate_id")

        gate = GateCreatePayload.from_dict(d.pop("gate"))

        success = d.pop("success", UNSET)

        was_created = d.pop("was_created", UNSET)

        gate_create_response = cls(
            gate_id=gate_id,
            gate=gate,
            success=success,
            was_created=was_created,
        )

        gate_create_response.additional_properties = d
        return gate_create_response

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
