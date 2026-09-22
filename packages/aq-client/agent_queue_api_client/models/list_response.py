from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.list_response_empty_reason_type_0 import ListResponseEmptyReasonType0
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
        variant_applied (str | Unset):  Default: 'active'.
        empty_reason (ListResponseEmptyReasonType0 | None | Unset):
    """

    layout_version: int
    nodes: list[LayoutNode] | Unset = UNSET
    next_cursor: None | str | Unset = UNSET
    variant_applied: str | Unset = "active"
    empty_reason: ListResponseEmptyReasonType0 | None | Unset = UNSET
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

        variant_applied = self.variant_applied

        empty_reason: None | str | Unset
        if isinstance(self.empty_reason, Unset):
            empty_reason = UNSET
        elif isinstance(self.empty_reason, ListResponseEmptyReasonType0):
            empty_reason = self.empty_reason.value
        else:
            empty_reason = self.empty_reason

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
        if variant_applied is not UNSET:
            field_dict["variant_applied"] = variant_applied
        if empty_reason is not UNSET:
            field_dict["empty_reason"] = empty_reason

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

        variant_applied = d.pop("variant_applied", UNSET)

        def _parse_empty_reason(data: object) -> ListResponseEmptyReasonType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                empty_reason_type_0 = ListResponseEmptyReasonType0(data)

                return empty_reason_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ListResponseEmptyReasonType0 | None | Unset, data)

        empty_reason = _parse_empty_reason(d.pop("empty_reason", UNSET))

        list_response = cls(
            layout_version=layout_version,
            nodes=nodes,
            next_cursor=next_cursor,
            variant_applied=variant_applied,
            empty_reason=empty_reason,
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
