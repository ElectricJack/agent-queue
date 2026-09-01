from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

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
        dep_type (str | Unset):  Default: 'blocks'.
        reason (None | str | Unset):
    """

    task_id: str
    depends_on: str
    task_title: str
    depends_on_title: str
    ok: bool | Unset = True
    dep_type: str | Unset = "blocks"
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        depends_on = self.depends_on

        task_title = self.task_title

        depends_on_title = self.depends_on_title

        ok = self.ok

        dep_type = self.dep_type

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

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
        if dep_type is not UNSET:
            field_dict["dep_type"] = dep_type
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        depends_on = d.pop("depends_on")

        task_title = d.pop("task_title")

        depends_on_title = d.pop("depends_on_title")

        ok = d.pop("ok", UNSET)

        dep_type = d.pop("dep_type", UNSET)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        add_dependency_response = cls(
            task_id=task_id,
            depends_on=depends_on,
            task_title=task_title,
            depends_on_title=depends_on_title,
            ok=ok,
            dep_type=dep_type,
            reason=reason,
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
