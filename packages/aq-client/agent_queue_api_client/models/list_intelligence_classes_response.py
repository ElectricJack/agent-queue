from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.intelligence_class_model import IntelligenceClassModel


T = TypeVar("T", bound="ListIntelligenceClassesResponse")


@_attrs_define
class ListIntelligenceClassesResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        classes (list[IntelligenceClassModel] | Unset):
    """

    success: bool | Unset = True
    classes: list[IntelligenceClassModel] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        classes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.classes, Unset):
            classes = []
            for classes_item_data in self.classes:
                classes_item = classes_item_data.to_dict()
                classes.append(classes_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if classes is not UNSET:
            field_dict["classes"] = classes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.intelligence_class_model import IntelligenceClassModel

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _classes = d.pop("classes", UNSET)
        classes: list[IntelligenceClassModel] | Unset = UNSET
        if _classes is not UNSET:
            classes = []
            for classes_item_data in _classes:
                classes_item = IntelligenceClassModel.from_dict(classes_item_data)

                classes.append(classes_item)

        list_intelligence_classes_response = cls(
            success=success,
            classes=classes,
        )

        list_intelligence_classes_response.additional_properties = d
        return list_intelligence_classes_response

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
