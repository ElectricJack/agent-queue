from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GenerateReadmeRequest")


@_attrs_define
class GenerateReadmeRequest:
    """
    Attributes:
        project_id (str): Project ID
        name (str): Human-readable project name
        description (None | str | Unset): Project description
        tech_stack (None | str | Unset): Comma-separated technologies
    """

    project_id: str
    name: str
    description: None | str | Unset = UNSET
    tech_stack: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        name = self.name

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        tech_stack: None | str | Unset
        if isinstance(self.tech_stack, Unset):
            tech_stack = UNSET
        else:
            tech_stack = self.tech_stack

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "name": name,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if tech_stack is not UNSET:
            field_dict["tech_stack"] = tech_stack

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        name = d.pop("name")

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        def _parse_tech_stack(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        tech_stack = _parse_tech_stack(d.pop("tech_stack", UNSET))

        generate_readme_request = cls(
            project_id=project_id,
            name=name,
            description=description,
            tech_stack=tech_stack,
        )

        generate_readme_request.additional_properties = d
        return generate_readme_request

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
