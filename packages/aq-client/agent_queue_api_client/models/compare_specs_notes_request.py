from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CompareSpecsNotesRequest")


@_attrs_define
class CompareSpecsNotesRequest:
    """
    Attributes:
        project_id (str): Project ID
        specs_path (None | str | Unset): Override path to specs directory (optional)
    """

    project_id: str
    specs_path: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        specs_path: None | str | Unset
        if isinstance(self.specs_path, Unset):
            specs_path = UNSET
        else:
            specs_path = self.specs_path

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if specs_path is not UNSET:
            field_dict["specs_path"] = specs_path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_specs_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        specs_path = _parse_specs_path(d.pop("specs_path", UNSET))

        compare_specs_notes_request = cls(
            project_id=project_id,
            specs_path=specs_path,
        )

        compare_specs_notes_request.additional_properties = d
        return compare_specs_notes_request

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
