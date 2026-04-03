from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RestoreTaskResponse")


@_attrs_define
class RestoreTaskResponse:
    """
    Attributes:
        restored (str):
        title (str):
        new_status (str | Unset):  Default: 'DEFINED'.
    """

    restored: str
    title: str
    new_status: str | Unset = "DEFINED"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        restored = self.restored

        title = self.title

        new_status = self.new_status

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "restored": restored,
                "title": title,
            }
        )
        if new_status is not UNSET:
            field_dict["new_status"] = new_status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        restored = d.pop("restored")

        title = d.pop("title")

        new_status = d.pop("new_status", UNSET)

        restore_task_response = cls(
            restored=restored,
            title=title,
            new_status=new_status,
        )

        restore_task_response.additional_properties = d
        return restore_task_response

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
