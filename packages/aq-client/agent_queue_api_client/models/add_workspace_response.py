from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AddWorkspaceResponse")


@_attrs_define
class AddWorkspaceResponse:
    """
    Attributes:
        created (str):
        project_id (str):
        workspace_path (str):
        source_type (str | Unset):  Default: ''.
    """

    created: str
    project_id: str
    workspace_path: str
    source_type: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        project_id = self.project_id

        workspace_path = self.workspace_path

        source_type = self.source_type

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created": created,
                "project_id": project_id,
                "workspace_path": workspace_path,
            }
        )
        if source_type is not UNSET:
            field_dict["source_type"] = source_type

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        created = d.pop("created")

        project_id = d.pop("project_id")

        workspace_path = d.pop("workspace_path")

        source_type = d.pop("source_type", UNSET)

        add_workspace_response = cls(
            created=created,
            project_id=project_id,
            workspace_path=workspace_path,
            source_type=source_type,
        )

        add_workspace_response.additional_properties = d
        return add_workspace_response

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
