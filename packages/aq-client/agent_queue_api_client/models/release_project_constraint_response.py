from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReleaseProjectConstraintResponse")


@_attrs_define
class ReleaseProjectConstraintResponse:
    """
    Attributes:
        project_id (str):
        constraint_released (bool | Unset):  Default: False.
        fields (None | str | Unset):
        fields_released (list[str] | Unset):
        remaining_fields (list[str] | Unset):
    """

    project_id: str
    constraint_released: bool | Unset = False
    fields: None | str | Unset = UNSET
    fields_released: list[str] | Unset = UNSET
    remaining_fields: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        constraint_released = self.constraint_released

        fields: None | str | Unset
        if isinstance(self.fields, Unset):
            fields = UNSET
        else:
            fields = self.fields

        fields_released: list[str] | Unset = UNSET
        if not isinstance(self.fields_released, Unset):
            fields_released = self.fields_released

        remaining_fields: list[str] | Unset = UNSET
        if not isinstance(self.remaining_fields, Unset):
            remaining_fields = self.remaining_fields

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if constraint_released is not UNSET:
            field_dict["constraint_released"] = constraint_released
        if fields is not UNSET:
            field_dict["fields"] = fields
        if fields_released is not UNSET:
            field_dict["fields_released"] = fields_released
        if remaining_fields is not UNSET:
            field_dict["remaining_fields"] = remaining_fields

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        constraint_released = d.pop("constraint_released", UNSET)

        def _parse_fields(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        fields = _parse_fields(d.pop("fields", UNSET))

        fields_released = cast(list[str], d.pop("fields_released", UNSET))

        remaining_fields = cast(list[str], d.pop("remaining_fields", UNSET))

        release_project_constraint_response = cls(
            project_id=project_id,
            constraint_released=constraint_released,
            fields=fields,
            fields_released=fields_released,
            remaining_fields=remaining_fields,
        )

        release_project_constraint_response.additional_properties = d
        return release_project_constraint_response

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
