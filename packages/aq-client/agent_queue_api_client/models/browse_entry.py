from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="BrowseEntry")


@_attrs_define
class BrowseEntry:
    """
    Attributes:
        name (str):
        relative_path (str):
        is_directory (bool | Unset):  Default: True.
        is_git_repository (bool | Unset):  Default: False.
        selectable (bool | Unset):  Default: False.
    """

    name: str
    relative_path: str
    is_directory: bool | Unset = True
    is_git_repository: bool | Unset = False
    selectable: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        relative_path = self.relative_path

        is_directory = self.is_directory

        is_git_repository = self.is_git_repository

        selectable = self.selectable

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "relative_path": relative_path,
            }
        )
        if is_directory is not UNSET:
            field_dict["is_directory"] = is_directory
        if is_git_repository is not UNSET:
            field_dict["is_git_repository"] = is_git_repository
        if selectable is not UNSET:
            field_dict["selectable"] = selectable

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        relative_path = d.pop("relative_path")

        is_directory = d.pop("is_directory", UNSET)

        is_git_repository = d.pop("is_git_repository", UNSET)

        selectable = d.pop("selectable", UNSET)

        browse_entry = cls(
            name=name,
            relative_path=relative_path,
            is_directory=is_directory,
            is_git_repository=is_git_repository,
            selectable=selectable,
        )

        browse_entry.additional_properties = d
        return browse_entry

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
