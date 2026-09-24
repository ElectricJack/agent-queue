from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeleteIntelligenceClassResponse")


@_attrs_define
class DeleteIntelligenceClassResponse:
    """
    Attributes:
        class_id (str):
        retired_file (str):
        success (bool | Unset):  Default: True.
    """

    class_id: str
    retired_file: str
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        class_id = self.class_id

        retired_file = self.retired_file

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "class_id": class_id,
                "retired_file": retired_file,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        class_id = d.pop("class_id")

        retired_file = d.pop("retired_file")

        success = d.pop("success", UNSET)

        delete_intelligence_class_response = cls(
            class_id=class_id,
            retired_file=retired_file,
            success=success,
        )

        delete_intelligence_class_response.additional_properties = d
        return delete_intelligence_class_response

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
