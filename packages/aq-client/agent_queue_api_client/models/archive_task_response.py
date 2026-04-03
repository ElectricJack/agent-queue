from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ArchiveTaskResponse")


@_attrs_define
class ArchiveTaskResponse:
    """
    Attributes:
        archived (str):
        title (str):
        status (str | Unset):  Default: ''.
    """

    archived: str
    title: str
    status: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        archived = self.archived

        title = self.title

        status = self.status

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "archived": archived,
                "title": title,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        archived = d.pop("archived")

        title = d.pop("title")

        status = d.pop("status", UNSET)

        archive_task_response = cls(
            archived=archived,
            title=title,
            status=status,
        )

        archive_task_response.additional_properties = d
        return archive_task_response

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
