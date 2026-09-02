from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.layout_node import LayoutNode


T = TypeVar("T", bound="ListResponse")


@_attrs_define
class ListResponse:
    """
    Attributes:
        layout_version (int):
        nodes (list[LayoutNode] | Unset):
        next_cursor (None | str | Unset):
    """

    layout_version: int
    nodes: list[LayoutNode] | Unset = UNSET
    next_cursor: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        layout_version = self.layout_version

        nodes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.nodes, Unset):
            nodes = []
            for nodes_item_data in self.nodes:
                nodes_item = nodes_item_data.to_dict()
                nodes.append(nodes_item)

        next_cursor: None | str | Unset
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "layout_version": layout_version,
            }
        )
        if nodes is not UNSET:
            field_dict["nodes"] = nodes
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.layout_node import LayoutNode

        d = dict(src_dict)
        layout_version = d.pop("layout_version")

        _nodes = d.pop("nodes", UNSET)
        nodes: list[LayoutNode] | Unset = UNSET
        if _nodes is not UNSET:
            nodes = []
            for nodes_item_data in _nodes:
                nodes_item = LayoutNode.from_dict(nodes_item_data)

                nodes.append(nodes_item)

        def _parse_next_cursor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))

        list_response = cls(
            layout_version=layout_version,
            nodes=nodes,
            next_cursor=next_cursor,
        )

        list_response.additional_properties = d
        return list_response

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
