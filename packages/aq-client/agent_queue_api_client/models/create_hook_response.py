from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateHookResponse")


@_attrs_define
class CreateHookResponse:
    """
    Attributes:
        created (str):
        name (str):
        project_id (str | Unset):  Default: ''.
        note (str | Unset):  Default: ''.
    """

    created: str
    name: str
    project_id: str | Unset = ""
    note: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        name = self.name

        project_id = self.project_id

        note = self.note

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created": created,
                "name": name,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if note is not UNSET:
            field_dict["note"] = note

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        created = d.pop("created")

        name = d.pop("name")

        project_id = d.pop("project_id", UNSET)

        note = d.pop("note", UNSET)

        create_hook_response = cls(
            created=created,
            name=name,
            project_id=project_id,
            note=note,
        )

        create_hook_response.additional_properties = d
        return create_hook_response

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
