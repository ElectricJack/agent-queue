from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReparentTaskResponse")


@_attrs_define
class ReparentTaskResponse:
    """
    Attributes:
        success (bool):
        task_id (str):
        old_parent (None | str | Unset):
        new_parent (None | str | Unset):
    """

    success: bool
    task_id: str
    old_parent: None | str | Unset = UNSET
    new_parent: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        task_id = self.task_id

        old_parent: None | str | Unset
        if isinstance(self.old_parent, Unset):
            old_parent = UNSET
        else:
            old_parent = self.old_parent

        new_parent: None | str | Unset
        if isinstance(self.new_parent, Unset):
            new_parent = UNSET
        else:
            new_parent = self.new_parent

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
                "task_id": task_id,
            }
        )
        if old_parent is not UNSET:
            field_dict["old_parent"] = old_parent
        if new_parent is not UNSET:
            field_dict["new_parent"] = new_parent

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success")

        task_id = d.pop("task_id")

        def _parse_old_parent(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        old_parent = _parse_old_parent(d.pop("old_parent", UNSET))

        def _parse_new_parent(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        new_parent = _parse_new_parent(d.pop("new_parent", UNSET))

        reparent_task_response = cls(
            success=success,
            task_id=task_id,
            old_parent=old_parent,
            new_parent=new_parent,
        )

        reparent_task_response.additional_properties = d
        return reparent_task_response

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
