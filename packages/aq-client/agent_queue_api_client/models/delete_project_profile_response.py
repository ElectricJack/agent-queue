from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeleteProjectProfileResponse")


@_attrs_define
class DeleteProjectProfileResponse:
    """
    Attributes:
        deleted (str):
        project_id (str):
        agent_type (str):
        removed_paths (list[str] | Unset):
    """

    deleted: str
    project_id: str
    agent_type: str
    removed_paths: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        deleted = self.deleted

        project_id = self.project_id

        agent_type = self.agent_type

        removed_paths: list[str] | Unset = UNSET
        if not isinstance(self.removed_paths, Unset):
            removed_paths = self.removed_paths

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "deleted": deleted,
                "project_id": project_id,
                "agent_type": agent_type,
            }
        )
        if removed_paths is not UNSET:
            field_dict["removed_paths"] = removed_paths

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        deleted = d.pop("deleted")

        project_id = d.pop("project_id")

        agent_type = d.pop("agent_type")

        removed_paths = cast(list[str], d.pop("removed_paths", UNSET))

        delete_project_profile_response = cls(
            deleted=deleted,
            project_id=project_id,
            agent_type=agent_type,
            removed_paths=removed_paths,
        )

        delete_project_profile_response.additional_properties = d
        return delete_project_profile_response

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
