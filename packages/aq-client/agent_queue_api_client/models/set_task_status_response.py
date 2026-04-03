from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="SetTaskStatusResponse")


@_attrs_define
class SetTaskStatusResponse:
    """
    Attributes:
        task_id (str):
        old_status (str):
        new_status (str):
        title (str):
    """

    task_id: str
    old_status: str
    new_status: str
    title: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        old_status = self.old_status

        new_status = self.new_status

        title = self.title

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "old_status": old_status,
                "new_status": new_status,
                "title": title,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        old_status = d.pop("old_status")

        new_status = d.pop("new_status")

        title = d.pop("title")

        set_task_status_response = cls(
            task_id=task_id,
            old_status=old_status,
            new_status=new_status,
            title=title,
        )

        set_task_status_response.additional_properties = d
        return set_task_status_response

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
