from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.layout_rect import LayoutRect


T = TypeVar("T", bound="TilesRequest")


@_attrs_define
class TilesRequest:
    """
    Attributes:
        rect (LayoutRect):
        variant (str | Unset):  Default: 'active'.
        expanded (list[str] | Unset):
        root (None | str | Unset):
        max_depth (int | None | Unset):
        q (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
    """

    rect: LayoutRect
    variant: str | Unset = "active"
    expanded: list[str] | Unset = UNSET
    root: None | str | Unset = UNSET
    max_depth: int | None | Unset = UNSET
    q: str | Unset = ""
    status: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        rect = self.rect.to_dict()

        variant = self.variant

        expanded: list[str] | Unset = UNSET
        if not isinstance(self.expanded, Unset):
            expanded = self.expanded

        root: None | str | Unset
        if isinstance(self.root, Unset):
            root = UNSET
        else:
            root = self.root

        max_depth: int | None | Unset
        if isinstance(self.max_depth, Unset):
            max_depth = UNSET
        else:
            max_depth = self.max_depth

        q = self.q

        status = self.status

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "rect": rect,
            }
        )
        if variant is not UNSET:
            field_dict["variant"] = variant
        if expanded is not UNSET:
            field_dict["expanded"] = expanded
        if root is not UNSET:
            field_dict["root"] = root
        if max_depth is not UNSET:
            field_dict["max_depth"] = max_depth
        if q is not UNSET:
            field_dict["q"] = q
        if status is not UNSET:
            field_dict["status"] = status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.layout_rect import LayoutRect

        d = dict(src_dict)
        rect = LayoutRect.from_dict(d.pop("rect"))

        variant = d.pop("variant", UNSET)

        expanded = cast(list[str], d.pop("expanded", UNSET))

        def _parse_root(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        root = _parse_root(d.pop("root", UNSET))

        def _parse_max_depth(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_depth = _parse_max_depth(d.pop("max_depth", UNSET))

        q = d.pop("q", UNSET)

        status = d.pop("status", UNSET)

        tiles_request = cls(
            rect=rect,
            variant=variant,
            expanded=expanded,
            root=root,
            max_depth=max_depth,
            q=q,
            status=status,
        )

        tiles_request.additional_properties = d
        return tiles_request

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
