from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.playbook_graph_edge_edge_type import PlaybookGraphEdgeEdgeType
from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookGraphEdge")


@_attrs_define
class PlaybookGraphEdge:
    """One directed, labelled edge between two compiled nodes.

    Attributes:
        source (str):
        target (str):
        edge_type (PlaybookGraphEdgeEdgeType):
        label (str | Unset):  Default: ''.
    """

    source: str
    target: str
    edge_type: PlaybookGraphEdgeEdgeType
    label: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        source = self.source

        target = self.target

        edge_type = self.edge_type.value

        label = self.label

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "source": source,
                "target": target,
                "edge_type": edge_type,
            }
        )
        if label is not UNSET:
            field_dict["label"] = label

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source = d.pop("source")

        target = d.pop("target")

        edge_type = PlaybookGraphEdgeEdgeType(d.pop("edge_type"))

        label = d.pop("label", UNSET)

        playbook_graph_edge = cls(
            source=source,
            target=target,
            edge_type=edge_type,
            label=label,
        )

        playbook_graph_edge.additional_properties = d
        return playbook_graph_edge

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
