from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateProjectResponse")


@_attrs_define
class CreateProjectResponse:
    """
    Attributes:
        created (str):
        name (str):
        assignment_playbook_id (None | str | Unset):
    """

    created: str
    name: str
    assignment_playbook_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        name = self.name

        assignment_playbook_id: None | str | Unset
        if isinstance(self.assignment_playbook_id, Unset):
            assignment_playbook_id = UNSET
        else:
            assignment_playbook_id = self.assignment_playbook_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created": created,
                "name": name,
            }
        )
        if assignment_playbook_id is not UNSET:
            field_dict["assignment_playbook_id"] = assignment_playbook_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        created = d.pop("created")

        name = d.pop("name")

        def _parse_assignment_playbook_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        assignment_playbook_id = _parse_assignment_playbook_id(d.pop("assignment_playbook_id", UNSET))

        create_project_response = cls(
            created=created,
            name=name,
            assignment_playbook_id=assignment_playbook_id,
        )

        create_project_response.additional_properties = d
        return create_project_response

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
