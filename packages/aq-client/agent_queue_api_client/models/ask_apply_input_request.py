from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.ask_apply_input_request_expected import AskApplyInputRequestExpected


T = TypeVar("T", bound="AskApplyInputRequest")


@_attrs_define
class AskApplyInputRequest:
    """
    Attributes:
        ask_id (str):
        input_id (str):
        expected (AskApplyInputRequestExpected):
        option_key (str):
        interpretation (str):
        idempotency_key (str):
    """

    ask_id: str
    input_id: str
    expected: AskApplyInputRequestExpected
    option_key: str
    interpretation: str
    idempotency_key: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ask_id = self.ask_id

        input_id = self.input_id

        expected = self.expected.to_dict()

        option_key = self.option_key

        interpretation = self.interpretation

        idempotency_key = self.idempotency_key

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "ask_id": ask_id,
                "input_id": input_id,
                "expected": expected,
                "option_key": option_key,
                "interpretation": interpretation,
                "idempotency_key": idempotency_key,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_apply_input_request_expected import AskApplyInputRequestExpected

        d = dict(src_dict)
        ask_id = d.pop("ask_id")

        input_id = d.pop("input_id")

        expected = AskApplyInputRequestExpected.from_dict(d.pop("expected"))

        option_key = d.pop("option_key")

        interpretation = d.pop("interpretation")

        idempotency_key = d.pop("idempotency_key")

        ask_apply_input_request = cls(
            ask_id=ask_id,
            input_id=input_id,
            expected=expected,
            option_key=option_key,
            interpretation=interpretation,
            idempotency_key=idempotency_key,
        )

        ask_apply_input_request.additional_properties = d
        return ask_apply_input_request

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
