from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.get_config_schema_response_schema import GetConfigSchemaResponseSchema


T = TypeVar("T", bound="GetConfigSchemaResponse")


@_attrs_define
class GetConfigSchemaResponse:
    """
    Attributes:
        schema (GetConfigSchemaResponseSchema | Unset):
    """

    schema: GetConfigSchemaResponseSchema | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        schema: dict[str, Any] | Unset = UNSET
        if not isinstance(self.schema, Unset):
            schema = self.schema.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if schema is not UNSET:
            field_dict["schema"] = schema

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.get_config_schema_response_schema import GetConfigSchemaResponseSchema  # noqa: PLC0415

        d = dict(src_dict)
        _schema = d.pop("schema", UNSET)
        schema: GetConfigSchemaResponseSchema | Unset
        if isinstance(_schema, Unset):
            schema = UNSET
        else:
            schema = GetConfigSchemaResponseSchema.from_dict(_schema)

        get_config_schema_response = cls(
            schema=schema,
        )

        get_config_schema_response.additional_properties = d
        return get_config_schema_response

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
