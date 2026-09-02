from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="LocateRequest")


@_attrs_define
class LocateRequest:
    """Where the matches for a filter are, in the geometry the canvas draws.

    Carries ``expanded`` for the same reason ``tiles`` and ``list`` are POSTs:
    collapsing a container reflows everything after it, so a match's position
    depends on the viewer's expanded set and cannot be answered from the
    persisted layout alone.

        Attributes:
            variant (str | Unset):  Default: 'active'.
            expanded (list[str] | Unset):
            q (str | Unset):  Default: ''.
            status (str | Unset):  Default: ''.
            limit (int | Unset):  Default: 200.
    """

    variant: str | Unset = "active"
    expanded: list[str] | Unset = UNSET
    q: str | Unset = ""
    status: str | Unset = ""
    limit: int | Unset = 200
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        variant = self.variant

        expanded: list[str] | Unset = UNSET
        if not isinstance(self.expanded, Unset):
            expanded = self.expanded

        q = self.q

        status = self.status

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

        limit = d.pop("limit", UNSET)

        locate_request = cls(
            variant=variant,
            expanded=expanded,
            q=q,
            status=status,
            limit=limit,
        )

        locate_request.additional_properties = d
        return locate_request

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
