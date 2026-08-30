from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="EditIntelligenceClassConflictResponse")


@_attrs_define
class EditIntelligenceClassConflictResponse:
    """
    Attributes:
        error (str):
        error_code (Literal['revision_conflict']):
        current_revision (str):
    """

    error: str
    error_code: Literal["revision_conflict"]
    current_revision: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        error = self.error

        error_code = self.error_code

        current_revision = self.current_revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "error": error,
                "error_code": error_code,
                "current_revision": current_revision,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        error = d.pop("error")

        error_code = cast(Literal["revision_conflict"], d.pop("error_code"))
        if error_code != "revision_conflict":
            raise ValueError(f"error_code must match const 'revision_conflict', got '{error_code}'")

        current_revision = d.pop("current_revision")

        edit_intelligence_class_conflict_response = cls(
            error=error,
            error_code=error_code,
            current_revision=current_revision,
        )

        edit_intelligence_class_conflict_response.additional_properties = d
        return edit_intelligence_class_conflict_response

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
