from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.explanation_value import ExplanationValue


T = TypeVar("T", bound="ExplanationInput")


@_attrs_define
class ExplanationInput:
    """
    Attributes:
        field (str):
        label (str):
        value (ExplanationValue):
        required (bool | Unset):  Default: False.
    """

    field: str
    label: str
    value: ExplanationValue
    required: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        field = self.field

        label = self.label

        value = self.value.to_dict()

        required = self.required

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "field": field,
                "label": label,
                "value": value,
            }
        )
        if required is not UNSET:
            field_dict["required"] = required

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.explanation_value import ExplanationValue

        d = dict(src_dict)
        field = d.pop("field")

        label = d.pop("label")

        value = ExplanationValue.from_dict(d.pop("value"))

        required = d.pop("required", UNSET)

        explanation_input = cls(
            field=field,
            label=label,
            value=value,
            required=required,
        )

        explanation_input.additional_properties = d
        return explanation_input

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
