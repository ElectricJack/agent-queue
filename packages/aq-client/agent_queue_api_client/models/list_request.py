from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ListRequest")


@_attrs_define
class ListRequest:
    """
    Attributes:
        variant (str | Unset):  Default: 'active'.
        expanded (list[str] | Unset):
        q (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        cursor (None | str | Unset):
        limit (int | Unset):  Default: 50.
    """

    variant: str | Unset = "active"
    expanded: list[str] | Unset = UNSET
    q: str | Unset = ""
    status: str | Unset = ""
    cursor: None | str | Unset = UNSET
    limit: int | Unset = 50
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        variant = self.variant

        expanded: list[str] | Unset = UNSET
        if not isinstance(self.expanded, Unset):
            expanded = self.expanded

        q = self.q

        status = self.status

        cursor: None | str | Unset
        if isinstance(self.cursor, Unset):
            cursor = UNSET
        else:
            cursor = self.cursor

        limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if variant is not UNSET:
            field_dict["variant"] = variant
        if expanded is not UNSET:
            field_dict["expanded"] = expanded
        if q is not UNSET:
            field_dict["q"] = q
        if status is not UNSET:
            field_dict["status"] = status
        if cursor is not UNSET:
            field_dict["cursor"] = cursor
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        variant = d.pop("variant", UNSET)

        expanded = cast(list[str], d.pop("expanded", UNSET))

        q = d.pop("q", UNSET)

        status = d.pop("status", UNSET)

        def _parse_cursor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        cursor = _parse_cursor(d.pop("cursor", UNSET))

        limit = d.pop("limit", UNSET)

        list_request = cls(
            variant=variant,
            expanded=expanded,
            q=q,
            status=status,
            cursor=cursor,
            limit=limit,
        )

        list_request.additional_properties = d
        return list_request

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
