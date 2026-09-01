from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskRef")


@_attrs_define
class TaskRef:
    """Minimal task reference used in dependency lists, unblocked lists, etc.

    Attributes:
        id (str):
        title (str):
        status (str | Unset):  Default: ''.
        dep_type (None | str | Unset):
        reason (None | str | Unset):
    """

    id: str
    title: str
    status: str | Unset = ""
    dep_type: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        title = self.title

        status = self.status

        dep_type: None | str | Unset
        if isinstance(self.dep_type, Unset):
            dep_type = UNSET
        else:
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
                "id": id,
                "title": title,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if dep_type is not UNSET:
            field_dict["dep_type"] = dep_type
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        title = d.pop("title")

        status = d.pop("status", UNSET)

        def _parse_dep_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        dep_type = _parse_dep_type(d.pop("dep_type", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        task_ref = cls(
            id=id,
            title=title,
            status=status,
            dep_type=dep_type,
            reason=reason,
        )

        task_ref.additional_properties = d
        return task_ref

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
