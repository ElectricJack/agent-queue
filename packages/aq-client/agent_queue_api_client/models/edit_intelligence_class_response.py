from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.intelligence_class_model import IntelligenceClassModel


T = TypeVar("T", bound="EditIntelligenceClassResponse")


@_attrs_define
class EditIntelligenceClassResponse:
    """
    Attributes:
        intelligence_class (IntelligenceClassModel):
        success (bool | Unset):  Default: True.
    """

    intelligence_class: IntelligenceClassModel
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        intelligence_class = self.intelligence_class.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "intelligence_class": intelligence_class,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.intelligence_class_model import IntelligenceClassModel

        d = dict(src_dict)
        intelligence_class = IntelligenceClassModel.from_dict(d.pop("intelligence_class"))

        success = d.pop("success", UNSET)

        edit_intelligence_class_response = cls(
            intelligence_class=intelligence_class,
            success=success,
        )

        edit_intelligence_class_response.additional_properties = d
        return edit_intelligence_class_response

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
