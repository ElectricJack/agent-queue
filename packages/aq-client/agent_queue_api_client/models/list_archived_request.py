from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ListArchivedRequest")


@_attrs_define
class ListArchivedRequest:
    """
    Attributes:
        project_id (None | str | Unset): Filter by project (optional)
        limit (int | None | Unset): Max number of tasks to return (default 50)
    """

    project_id: None | str | Unset = UNSET
    limit: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        limit: int | None | Unset
        if isinstance(self.limit, Unset):
            limit = UNSET
        else:
            limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_limit(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        limit = _parse_limit(d.pop("limit", UNSET))

        list_archived_request = cls(
            project_id=project_id,
            limit=limit,
        )

        list_archived_request.additional_properties = d
        return list_archived_request

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
