from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.intelligence_class_reference import IntelligenceClassReference


T = TypeVar("T", bound="DeleteIntelligenceClassConflictResponse")


@_attrs_define
class DeleteIntelligenceClassConflictResponse:
    """
    Attributes:
        error (str):
        error_code (Literal['class_referenced']):
        references (list[IntelligenceClassReference]):
    """

    error: str
    error_code: Literal["class_referenced"]
    references: list[IntelligenceClassReference]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        error = self.error

        error_code = self.error_code

        references = []
        for references_item_data in self.references:
            references_item = references_item_data.to_dict()
            references.append(references_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "error": error,
                "error_code": error_code,
                "references": references,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.intelligence_class_reference import IntelligenceClassReference

        d = dict(src_dict)
        error = d.pop("error")

        error_code = cast(Literal["class_referenced"], d.pop("error_code"))
        if error_code != "class_referenced":
            raise ValueError(f"error_code must match const 'class_referenced', got '{error_code}'")

        references = []
        _references = d.pop("references")
        for references_item_data in _references:
            references_item = IntelligenceClassReference.from_dict(references_item_data)

            references.append(references_item)

        delete_intelligence_class_conflict_response = cls(
            error=error,
            error_code=error_code,
            references=references,
        )

        delete_intelligence_class_conflict_response.additional_properties = d
        return delete_intelligence_class_conflict_response

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
