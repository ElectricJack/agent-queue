from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AddDependencyResponse")


@_attrs_define
class AddDependencyResponse:
    """
    Attributes:
        task_id (str):
        depends_on (str):
        task_title (str):
        depends_on_title (str):
        ok (bool | Unset):  Default: True.
    """

    task_id: str
    depends_on: str
    task_title: str
    depends_on_title: str
    ok: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        depends_on = self.depends_on

        task_title = self.task_title

        depends_on_title = self.depends_on_title

        ok = self.ok

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "depends_on": depends_on,
                "task_title": task_title,
                "depends_on_title": depends_on_title,
            }
        )
        if ok is not UNSET:
            field_dict["ok"] = ok

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        depends_on = d.pop("depends_on")

        task_title = d.pop("task_title")

        depends_on_title = d.pop("depends_on_title")

        ok = d.pop("ok", UNSET)

        add_dependency_response = cls(
            task_id=task_id,
            depends_on=depends_on,
            task_title=task_title,
            depends_on_title=depends_on_title,
            ok=ok,
        )

        add_dependency_response.additional_properties = d
        return add_dependency_response

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
