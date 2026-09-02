from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.probed_tool_model_input_schema import ProbedToolModelInputSchema


T = TypeVar("T", bound="ProbedToolModel")


@_attrs_define
class ProbedToolModel:
    """
    Attributes:
        name (str):
        description (str | Unset):  Default: ''.
        input_schema (ProbedToolModelInputSchema | Unset):
    """

    name: str
    description: str | Unset = ""
    input_schema: ProbedToolModelInputSchema | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        description = self.description

        input_schema: dict[str, Any] | Unset = UNSET
        if not isinstance(self.input_schema, Unset):
            input_schema = self.input_schema.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if input_schema is not UNSET:
            field_dict["input_schema"] = input_schema

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.probed_tool_model_input_schema import ProbedToolModelInputSchema

        d = dict(src_dict)
        name = d.pop("name")

        description = d.pop("description", UNSET)

        _input_schema = d.pop("input_schema", UNSET)
        input_schema: ProbedToolModelInputSchema | Unset
        if isinstance(_input_schema, Unset):
            input_schema = UNSET
        else:
            input_schema = ProbedToolModelInputSchema.from_dict(_input_schema)

        probed_tool_model = cls(
            name=name,
            description=description,
            input_schema=input_schema,
        )

        probed_tool_model.additional_properties = d
        return probed_tool_model

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
