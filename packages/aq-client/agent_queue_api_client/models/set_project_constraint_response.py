from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SetProjectConstraintResponse")


@_attrs_define
class SetProjectConstraintResponse:
    """
    Attributes:
        project_id (str):
        constraint_set (bool | Unset):  Default: False.
        active_fields (list[str] | Unset):
    """

    project_id: str
    constraint_set: bool | Unset = False
    active_fields: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        constraint_set = self.constraint_set

        active_fields: list[str] | Unset = UNSET
        if not isinstance(self.active_fields, Unset):
            active_fields = self.active_fields

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if constraint_set is not UNSET:
            field_dict["constraint_set"] = constraint_set
        if active_fields is not UNSET:
            field_dict["active_fields"] = active_fields

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        constraint_set = d.pop("constraint_set", UNSET)

        active_fields = cast(list[str], d.pop("active_fields", UNSET))

        set_project_constraint_response = cls(
            project_id=project_id,
            constraint_set=constraint_set,
            active_fields=active_fields,
        )

        set_project_constraint_response.additional_properties = d
        return set_project_constraint_response

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
