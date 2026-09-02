from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="LayoutEdge")


@_attrs_define
class LayoutEdge:
    """
    Attributes:
        from_ (str):
        to (str):
        dep_type (str):
        description (None | str | Unset):
        count (int | Unset):  Default: 1.
    """

    from_: str
    to: str
    dep_type: str
    description: None | str | Unset = UNSET
    count: int | Unset = 1
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from_ = self.from_

        to = self.to

        dep_type = self.dep_type

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        count = self.count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "from": from_,
                "to": to,
                "dep_type": dep_type,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if count is not UNSET:
            field_dict["count"] = count

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        from_ = d.pop("from")

        to = d.pop("to")

        dep_type = d.pop("dep_type")

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        count = d.pop("count", UNSET)

        layout_edge = cls(
            from_=from_,
            to=to,
            dep_type=dep_type,
            description=description,
            count=count,
        )

        layout_edge.additional_properties = d
        return layout_edge

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
