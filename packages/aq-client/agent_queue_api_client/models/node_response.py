from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ancestor_ref import AncestorRef
    from ..models.layout_node import LayoutNode


T = TypeVar("T", bound="NodeResponse")


@_attrs_define
class NodeResponse:
    """
    Attributes:
        node (LayoutNode):
        layout_version (int):
        ancestors (list[AncestorRef] | Unset):
    """

    node: LayoutNode
    layout_version: int
    ancestors: list[AncestorRef] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        node = self.node.to_dict()

        layout_version = self.layout_version

        ancestors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.ancestors, Unset):
            ancestors = []
            for ancestors_item_data in self.ancestors:
                ancestors_item = ancestors_item_data.to_dict()
                ancestors.append(ancestors_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "node": node,
                "layout_version": layout_version,
            }
        )
        if ancestors is not UNSET:
            field_dict["ancestors"] = ancestors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ancestor_ref import AncestorRef
        from ..models.layout_node import LayoutNode

        d = dict(src_dict)
        node = LayoutNode.from_dict(d.pop("node"))

        layout_version = d.pop("layout_version")

        _ancestors = d.pop("ancestors", UNSET)
        ancestors: list[AncestorRef] | Unset = UNSET
        if _ancestors is not UNSET:
            ancestors = []
            for ancestors_item_data in _ancestors:
                ancestors_item = AncestorRef.from_dict(ancestors_item_data)

                ancestors.append(ancestors_item)

        node_response = cls(
            node=node,
            layout_version=layout_version,
            ancestors=ancestors,
        )

        node_response.additional_properties = d
        return node_response

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
