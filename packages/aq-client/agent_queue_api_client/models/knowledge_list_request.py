from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="KnowledgeListRequest")


@_attrs_define
class KnowledgeListRequest:
    """
    Attributes:
        project_id (str):
        category (None | str | Unset):
        include_retired (bool | Unset):  Default: False.
        include_disputed (bool | Unset):  Default: False.
        limit (int | Unset):  Default: 25.
        cursor (None | str | Unset):
    """

    project_id: str
    category: None | str | Unset = UNSET
    include_retired: bool | Unset = False
    include_disputed: bool | Unset = False
    limit: int | Unset = 25
    cursor: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        category: None | str | Unset
        if isinstance(self.category, Unset):
            category = UNSET
        else:
            category = self.category

        include_retired = self.include_retired

        include_disputed = self.include_disputed

        limit = self.limit

        cursor: None | str | Unset
        if isinstance(self.cursor, Unset):
            cursor = UNSET
        else:
            cursor = self.cursor

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if category is not UNSET:
            field_dict["category"] = category
        if include_retired is not UNSET:
            field_dict["include_retired"] = include_retired
        if include_disputed is not UNSET:
            field_dict["include_disputed"] = include_disputed
        if limit is not UNSET:
            field_dict["limit"] = limit
        if cursor is not UNSET:
            field_dict["cursor"] = cursor

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_category(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        category = _parse_category(d.pop("category", UNSET))

        include_retired = d.pop("include_retired", UNSET)

        include_disputed = d.pop("include_disputed", UNSET)

        limit = d.pop("limit", UNSET)

        def _parse_cursor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        cursor = _parse_cursor(d.pop("cursor", UNSET))

        knowledge_list_request = cls(
            project_id=project_id,
            category=category,
            include_retired=include_retired,
            include_disputed=include_disputed,
            limit=limit,
            cursor=cursor,
        )

        knowledge_list_request.additional_properties = d
        return knowledge_list_request

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
