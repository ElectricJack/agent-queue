from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RemoveWorkspaceResponse")


@_attrs_define
class RemoveWorkspaceResponse:
    """
    Attributes:
        deleted (str):
        name (None | str | Unset):
        project_id (str | Unset):  Default: ''.
        workspace_path (str | Unset):  Default: ''.
    """

    deleted: str
    name: None | str | Unset = UNSET
    project_id: str | Unset = ""
    workspace_path: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        deleted = self.deleted

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        project_id = self.project_id

        workspace_path = self.workspace_path

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "deleted": deleted,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if workspace_path is not UNSET:
            field_dict["workspace_path"] = workspace_path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        deleted = d.pop("deleted")

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        project_id = d.pop("project_id", UNSET)

        workspace_path = d.pop("workspace_path", UNSET)

        remove_workspace_response = cls(
            deleted=deleted,
            name=name,
            project_id=project_id,
            workspace_path=workspace_path,
        )

        remove_workspace_response.additional_properties = d
        return remove_workspace_response

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
