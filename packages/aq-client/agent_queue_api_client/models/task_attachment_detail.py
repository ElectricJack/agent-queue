from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="TaskAttachmentDetail")


@_attrs_define
class TaskAttachmentDetail:
    """
    Attributes:
        id (str):
        name (str):
        path (str):
        url (str):
        content_type (str):
        size (int):
    """

    id: str
    name: str
    path: str
    url: str
    content_type: str
    size: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        path = self.path

        url = self.url

        content_type = self.content_type

        size = self.size

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
                "path": path,
                "url": url,
                "content_type": content_type,
                "size": size,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        path = d.pop("path")

        url = d.pop("url")

        content_type = d.pop("content_type")

        size = d.pop("size")

        task_attachment_detail = cls(
            id=id,
            name=name,
            path=path,
            url=url,
            content_type=content_type,
            size=size,
        )

        task_attachment_detail.additional_properties = d
        return task_attachment_detail

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
