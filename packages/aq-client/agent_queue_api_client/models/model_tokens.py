from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ModelTokens")


@_attrs_define
class ModelTokens:
    """
    Attributes:
        input_per_min (float | Unset):  Default: 0.0.
        output_per_min (float | Unset):  Default: 0.0.
    """

    input_per_min: float | Unset = 0.0
    output_per_min: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        input_per_min = self.input_per_min

        output_per_min = self.output_per_min

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if input_per_min is not UNSET:
            field_dict["input_per_min"] = input_per_min
        if output_per_min is not UNSET:
            field_dict["output_per_min"] = output_per_min

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        input_per_min = d.pop("input_per_min", UNSET)

        output_per_min = d.pop("output_per_min", UNSET)

        model_tokens = cls(
            input_per_min=input_per_min,
            output_per_min=output_per_min,
        )

        model_tokens.additional_properties = d
        return model_tokens

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
