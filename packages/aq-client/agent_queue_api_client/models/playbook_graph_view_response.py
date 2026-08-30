from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_graph_view_response_edges_item import PlaybookGraphViewResponseEdgesItem
    from ..models.playbook_graph_view_response_nodes_item import PlaybookGraphViewResponseNodesItem
    from ..models.playbook_graph_view_response_overlays import PlaybookGraphViewResponseOverlays


T = TypeVar("T", bound="PlaybookGraphViewResponse")


@_attrs_define
class PlaybookGraphViewResponse:
    """Build-graph-view output — keeps the wire shape loose because the
    payload is consumed wholesale by the dashboard renderer.

        Attributes:
            success (bool | Unset):  Default: True.
            playbook_id (str | Unset):  Default: ''.
            nodes (list[PlaybookGraphViewResponseNodesItem] | Unset):
            edges (list[PlaybookGraphViewResponseEdgesItem] | Unset):
            direction (str | Unset):  Default: 'TD'.
            overlays (PlaybookGraphViewResponseOverlays | Unset):
    """

    success: bool | Unset = True
    playbook_id: str | Unset = ""
    nodes: list[PlaybookGraphViewResponseNodesItem] | Unset = UNSET
    edges: list[PlaybookGraphViewResponseEdgesItem] | Unset = UNSET
    direction: str | Unset = "TD"
    overlays: PlaybookGraphViewResponseOverlays | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        playbook_id = self.playbook_id

        nodes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.nodes, Unset):
            nodes = []
            for nodes_item_data in self.nodes:
                nodes_item = nodes_item_data.to_dict()
                nodes.append(nodes_item)

        edges: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.edges, Unset):
            edges = []
            for edges_item_data in self.edges:
                edges_item = edges_item_data.to_dict()
                edges.append(edges_item)

        direction = self.direction

        overlays: dict[str, Any] | Unset = UNSET
        if not isinstance(self.overlays, Unset):
            overlays = self.overlays.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if playbook_id is not UNSET:
            field_dict["playbook_id"] = playbook_id
        if nodes is not UNSET:
            field_dict["nodes"] = nodes
        if edges is not UNSET:
            field_dict["edges"] = edges
        if direction is not UNSET:
            field_dict["direction"] = direction
        if overlays is not UNSET:
            field_dict["overlays"] = overlays

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_graph_view_response_edges_item import PlaybookGraphViewResponseEdgesItem
        from ..models.playbook_graph_view_response_nodes_item import PlaybookGraphViewResponseNodesItem
        from ..models.playbook_graph_view_response_overlays import PlaybookGraphViewResponseOverlays

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        playbook_id = d.pop("playbook_id", UNSET)

        _nodes = d.pop("nodes", UNSET)
        nodes: list[PlaybookGraphViewResponseNodesItem] | Unset = UNSET
        if _nodes is not UNSET:
            nodes = []
            for nodes_item_data in _nodes:
                nodes_item = PlaybookGraphViewResponseNodesItem.from_dict(nodes_item_data)

                nodes.append(nodes_item)

        _edges = d.pop("edges", UNSET)
        edges: list[PlaybookGraphViewResponseEdgesItem] | Unset = UNSET
        if _edges is not UNSET:
            edges = []
            for edges_item_data in _edges:
                edges_item = PlaybookGraphViewResponseEdgesItem.from_dict(edges_item_data)

                edges.append(edges_item)

        direction = d.pop("direction", UNSET)

        _overlays = d.pop("overlays", UNSET)
        overlays: PlaybookGraphViewResponseOverlays | Unset
        if isinstance(_overlays, Unset):
            overlays = UNSET
        else:
            overlays = PlaybookGraphViewResponseOverlays.from_dict(_overlays)

        playbook_graph_view_response = cls(
            success=success,
            playbook_id=playbook_id,
            nodes=nodes,
            edges=edges,
            direction=direction,
            overlays=overlays,
        )

        playbook_graph_view_response.additional_properties = d
        return playbook_graph_view_response

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
