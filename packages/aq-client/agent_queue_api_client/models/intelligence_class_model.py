from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.intelligence_class_model_mapping import IntelligenceClassModelMapping


T = TypeVar("T", bound="IntelligenceClassModel")


@_attrs_define
class IntelligenceClassModel:
    """
    Attributes:
        id (str):
        revision (str):
        name (str | Unset):  Default: ''.
        description (str | Unset):  Default: ''.
        mapping (IntelligenceClassModelMapping | Unset):
        loaded (bool | Unset):  Default: True.
    """

    id: str
    revision: str
    name: str | Unset = ""
    description: str | Unset = ""
    mapping: IntelligenceClassModelMapping | Unset = UNSET
    loaded: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        revision = self.revision

        name = self.name

        description = self.description

        mapping: dict[str, Any] | Unset = UNSET
        if not isinstance(self.mapping, Unset):
            mapping = self.mapping.to_dict()

        loaded = self.loaded

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "revision": revision,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if description is not UNSET:
            field_dict["description"] = description
        if mapping is not UNSET:
            field_dict["mapping"] = mapping
        if loaded is not UNSET:
            field_dict["loaded"] = loaded

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.intelligence_class_model_mapping import IntelligenceClassModelMapping

        d = dict(src_dict)
        id = d.pop("id")

        revision = d.pop("revision")

        name = d.pop("name", UNSET)

        description = d.pop("description", UNSET)

        _mapping = d.pop("mapping", UNSET)
        mapping: IntelligenceClassModelMapping | Unset
        if isinstance(_mapping, Unset):
            mapping = UNSET
        else:
            mapping = IntelligenceClassModelMapping.from_dict(_mapping)

        loaded = d.pop("loaded", UNSET)

        intelligence_class_model = cls(
            id=id,
            revision=revision,
            name=name,
            description=description,
            mapping=mapping,
            loaded=loaded,
        )

        intelligence_class_model.additional_properties = d
        return intelligence_class_model

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
