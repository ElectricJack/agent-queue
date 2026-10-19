from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.task_dict_status import TaskDictStatus
from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskDict")


@_attrs_define
class TaskDict:
    """Loose task dict as returned in list results.

    Attributes:
        id (str):
        title (str | Unset):  Default: ''.
        status (TaskDictStatus | Unset):  Default: TaskDictStatus.VALUE_0.
    """

    id: str
    title: str | Unset = ""
    status: TaskDictStatus | Unset = TaskDictStatus.VALUE_0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        title = self.title

        status: str | Unset = UNSET
        if not isinstance(self.status, Unset):
            status = self.status.value

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
            }
        )
        if title is not UNSET:
            field_dict["title"] = title
        if status is not UNSET:
            field_dict["status"] = status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        title = d.pop("title", UNSET)

        _status = d.pop("status", UNSET)
        status: TaskDictStatus | Unset
        if isinstance(_status, Unset):
            status = UNSET
        else:
            status = TaskDictStatus(_status)

        task_dict = cls(
            id=id,
            title=title,
            status=status,
        )

        task_dict.additional_properties = d
        return task_dict

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
