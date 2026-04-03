from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ListActiveTasksAllProjectsRequest")


@_attrs_define
class ListActiveTasksAllProjectsRequest:
    """
    Attributes:
        include_completed (bool | None | Unset): When true, include completed/failed/blocked tasks too. Default false
            (active tasks only).
    """

    include_completed: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        include_completed: bool | None | Unset
        if isinstance(self.include_completed, Unset):
            include_completed = UNSET
        else:
            include_completed = self.include_completed

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if include_completed is not UNSET:
            field_dict["include_completed"] = include_completed

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_include_completed(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        include_completed = _parse_include_completed(d.pop("include_completed", UNSET))

        list_active_tasks_all_projects_request = cls(
            include_completed=include_completed,
        )

        list_active_tasks_all_projects_request.additional_properties = d
        return list_active_tasks_all_projects_request

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
