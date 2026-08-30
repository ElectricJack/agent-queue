from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EditWorkspaceResponse")


@_attrs_define
class EditWorkspaceResponse:
    """
    Attributes:
        updated (str):
        fields (list[str] | Unset):
        workspace_path (str | Unset):  Default: ''.
        source_type (str | Unset):  Default: ''.
        enabled (bool | Unset):  Default: True.
    """

    updated: str
    fields: list[str] | Unset = UNSET
    workspace_path: str | Unset = ""
    source_type: str | Unset = ""
    enabled: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        updated = self.updated

        fields: list[str] | Unset = UNSET
        if not isinstance(self.fields, Unset):
            fields = self.fields

        workspace_path = self.workspace_path

        source_type = self.source_type

        enabled = self.enabled

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "updated": updated,
            }
        )
        if fields is not UNSET:
            field_dict["fields"] = fields
        if workspace_path is not UNSET:
            field_dict["workspace_path"] = workspace_path
        if source_type is not UNSET:
            field_dict["source_type"] = source_type
        if enabled is not UNSET:
            field_dict["enabled"] = enabled

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        updated = d.pop("updated")

        fields = cast(list[str], d.pop("fields", UNSET))

        workspace_path = d.pop("workspace_path", UNSET)

        source_type = d.pop("source_type", UNSET)

        enabled = d.pop("enabled", UNSET)

        edit_workspace_response = cls(
            updated=updated,
            fields=fields,
            workspace_path=workspace_path,
            source_type=source_type,
            enabled=enabled,
        )

        edit_workspace_response.additional_properties = d
        return edit_workspace_response

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
