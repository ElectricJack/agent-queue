from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeleteNoteResponse")


@_attrs_define
class DeleteNoteResponse:
    """
    Attributes:
        deleted (str):
        title (str | Unset):  Default: ''.
    """

    deleted: str
    title: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        deleted = self.deleted

        title = self.title

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "deleted": deleted,
            }
        )
        if title is not UNSET:
            field_dict["title"] = title

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        deleted = d.pop("deleted")

        title = d.pop("title", UNSET)

        delete_note_response = cls(
            deleted=deleted,
            title=title,
        )

        delete_note_response.additional_properties = d
        return delete_note_response

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
