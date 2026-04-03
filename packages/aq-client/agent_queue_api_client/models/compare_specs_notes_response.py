from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.note_summary import NoteSummary


T = TypeVar("T", bound="CompareSpecsNotesResponse")


@_attrs_define
class CompareSpecsNotesResponse:
    """
    Attributes:
        specs (list[NoteSummary] | Unset):
        notes (list[NoteSummary] | Unset):
        specs_path (str | Unset):  Default: ''.
        notes_path (str | Unset):  Default: ''.
        project_id (str | Unset):  Default: ''.
    """

    specs: list[NoteSummary] | Unset = UNSET
    notes: list[NoteSummary] | Unset = UNSET
    specs_path: str | Unset = ""
    notes_path: str | Unset = ""
    project_id: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        specs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.specs, Unset):
            specs = []
            for specs_item_data in self.specs:
                specs_item = specs_item_data.to_dict()
                specs.append(specs_item)

        notes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.notes, Unset):
            notes = []
            for notes_item_data in self.notes:
                notes_item = notes_item_data.to_dict()
                notes.append(notes_item)

        specs_path = self.specs_path

        notes_path = self.notes_path

        project_id = self.project_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if specs is not UNSET:
            field_dict["specs"] = specs
        if notes is not UNSET:
            field_dict["notes"] = notes
        if specs_path is not UNSET:
            field_dict["specs_path"] = specs_path
        if notes_path is not UNSET:
            field_dict["notes_path"] = notes_path
        if project_id is not UNSET:
            field_dict["project_id"] = project_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.note_summary import NoteSummary

        d = dict(src_dict)
        _specs = d.pop("specs", UNSET)
        specs: list[NoteSummary] | Unset = UNSET
        if _specs is not UNSET:
            specs = []
            for specs_item_data in _specs:
                specs_item = NoteSummary.from_dict(specs_item_data)

                specs.append(specs_item)

        _notes = d.pop("notes", UNSET)
        notes: list[NoteSummary] | Unset = UNSET
        if _notes is not UNSET:
            notes = []
            for notes_item_data in _notes:
                notes_item = NoteSummary.from_dict(notes_item_data)

                notes.append(notes_item)

        specs_path = d.pop("specs_path", UNSET)

        notes_path = d.pop("notes_path", UNSET)

        project_id = d.pop("project_id", UNSET)

        compare_specs_notes_response = cls(
            specs=specs,
            notes=notes,
            specs_path=specs_path,
            notes_path=notes_path,
            project_id=project_id,
        )

        compare_specs_notes_response.additional_properties = d
        return compare_specs_notes_response

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
