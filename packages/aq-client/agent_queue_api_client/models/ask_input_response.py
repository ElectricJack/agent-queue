from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_input_response_receipt import AskInputResponseReceipt


T = TypeVar("T", bound="AskInputResponse")


@_attrs_define
class AskInputResponse:
    """
    Attributes:
        created (bool):
        input_id (str):
        receipt (AskInputResponseReceipt):
        success (bool | Unset):  Default: True.
    """

    created: bool
    input_id: str
    receipt: AskInputResponseReceipt
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        input_id = self.input_id

        receipt = self.receipt.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created": created,
                "input_id": input_id,
                "receipt": receipt,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_input_response_receipt import AskInputResponseReceipt

        d = dict(src_dict)
        created = d.pop("created")

        input_id = d.pop("input_id")

        receipt = AskInputResponseReceipt.from_dict(d.pop("receipt"))

        success = d.pop("success", UNSET)

        ask_input_response = cls(
            created=created,
            input_id=input_id,
            receipt=receipt,
            success=success,
        )

        ask_input_response.additional_properties = d
        return ask_input_response

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
