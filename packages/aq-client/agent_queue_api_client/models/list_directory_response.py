from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.file_entry import FileEntry


T = TypeVar("T", bound="ListDirectoryResponse")


@_attrs_define
class ListDirectoryResponse:
    """
    Attributes:
        project_id (str | Unset):  Default: ''.
        path (str | Unset):  Default: ''.
        workspace_path (str | Unset):  Default: ''.
        workspace_name (str | Unset):  Default: ''.
        directories (list[str] | Unset):
        files (list[FileEntry] | Unset):
    """

    project_id: str | Unset = ""
    path: str | Unset = ""
    workspace_path: str | Unset = ""
    workspace_name: str | Unset = ""
    directories: list[str] | Unset = UNSET
    files: list[FileEntry] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        path = self.path

        workspace_path = self.workspace_path

        workspace_name = self.workspace_name

        directories: list[str] | Unset = UNSET
        if not isinstance(self.directories, Unset):
            directories = self.directories

        files: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.files, Unset):
            files = []
            for files_item_data in self.files:
                files_item = files_item_data.to_dict()
                files.append(files_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if path is not UNSET:
            field_dict["path"] = path
        if workspace_path is not UNSET:
            field_dict["workspace_path"] = workspace_path
        if workspace_name is not UNSET:
            field_dict["workspace_name"] = workspace_name
        if directories is not UNSET:
            field_dict["directories"] = directories
        if files is not UNSET:
            field_dict["files"] = files

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.file_entry import FileEntry

        d = dict(src_dict)
        project_id = d.pop("project_id", UNSET)

        path = d.pop("path", UNSET)

        workspace_path = d.pop("workspace_path", UNSET)

        workspace_name = d.pop("workspace_name", UNSET)

        directories = cast(list[str], d.pop("directories", UNSET))

        _files = d.pop("files", UNSET)
        files: list[FileEntry] | Unset = UNSET
        if _files is not UNSET:
            files = []
            for files_item_data in _files:
                files_item = FileEntry.from_dict(files_item_data)

                files.append(files_item)

        list_directory_response = cls(
            project_id=project_id,
            path=path,
            workspace_path=workspace_path,
            workspace_name=workspace_name,
            directories=directories,
            files=files,
        )

        list_directory_response.additional_properties = d
        return list_directory_response

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
