from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeleteProjectRequest")


@_attrs_define
class DeleteProjectRequest:
    """
    Attributes:
        project_id (str): Project ID to delete
        archive_channels (bool | Unset): If true, archive the project's Discord channels (rename + set read-only)
            instead of leaving them as-is. Default: false. Default: False.
    """

    project_id: str
    archive_channels: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        archive_channels = self.archive_channels

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if archive_channels is not UNSET:
            field_dict["archive_channels"] = archive_channels

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        archive_channels = d.pop("archive_channels", UNSET)

        delete_project_request = cls(
            project_id=project_id,
            archive_channels=archive_channels,
        )

        delete_project_request.additional_properties = d
        return delete_project_request

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
