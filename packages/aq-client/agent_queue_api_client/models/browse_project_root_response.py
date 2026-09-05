from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.browse_entry import BrowseEntry


T = TypeVar("T", bound="BrowseProjectRootResponse")


@_attrs_define
class BrowseProjectRootResponse:
    """
    Attributes:
        root_id (str):
        success (bool | Unset):  Default: True.
        relative_path (str | Unset):  Default: ''.
        entries (list[BrowseEntry] | Unset):
        truncated (bool | Unset):  Default: False.
    """

    root_id: str
    success: bool | Unset = True
    relative_path: str | Unset = ""
    entries: list[BrowseEntry] | Unset = UNSET
    truncated: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        root_id = self.root_id

        success = self.success

        relative_path = self.relative_path

        entries: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.entries, Unset):
            entries = []
            for entries_item_data in self.entries:
                entries_item = entries_item_data.to_dict()
                entries.append(entries_item)

        truncated = self.truncated

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "root_id": root_id,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if relative_path is not UNSET:
            field_dict["relative_path"] = relative_path
        if entries is not UNSET:
            field_dict["entries"] = entries
        if truncated is not UNSET:
            field_dict["truncated"] = truncated

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.browse_entry import BrowseEntry

        d = dict(src_dict)
        root_id = d.pop("root_id")

        success = d.pop("success", UNSET)

        relative_path = d.pop("relative_path", UNSET)

        _entries = d.pop("entries", UNSET)
        entries: list[BrowseEntry] | Unset = UNSET
        if _entries is not UNSET:
            entries = []
            for entries_item_data in _entries:
                entries_item = BrowseEntry.from_dict(entries_item_data)

                entries.append(entries_item)

        truncated = d.pop("truncated", UNSET)

        browse_project_root_response = cls(
            root_id=root_id,
            success=success,
            relative_path=relative_path,
            entries=entries,
            truncated=truncated,
        )

        browse_project_root_response.additional_properties = d
        return browse_project_root_response

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
