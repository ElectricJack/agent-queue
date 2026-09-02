from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.graph_gate import GraphGate
    from ..models.layout_edge import LayoutEdge
    from ..models.layout_node import LayoutNode
    from ..models.layout_stub import LayoutStub
    from ..models.layout_worker import LayoutWorker
    from ..models.stub_overflow import StubOverflow


T = TypeVar("T", bound="TilesResponse")


@_attrs_define
class TilesResponse:
    """
    Attributes:
        layout_version (int):
        nodes (list[LayoutNode] | Unset):
        edges (list[LayoutEdge] | Unset):
        stubs (list[LayoutStub] | Unset):
        stub_overflow (list[StubOverflow] | Unset):
        workers (list[LayoutWorker] | Unset):
        gates (list[GraphGate] | Unset):
    """

    layout_version: int
    nodes: list[LayoutNode] | Unset = UNSET
    edges: list[LayoutEdge] | Unset = UNSET
    stubs: list[LayoutStub] | Unset = UNSET
    stub_overflow: list[StubOverflow] | Unset = UNSET
    workers: list[LayoutWorker] | Unset = UNSET
    gates: list[GraphGate] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        layout_version = self.layout_version

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

        stubs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.stubs, Unset):
            stubs = []
            for stubs_item_data in self.stubs:
                stubs_item = stubs_item_data.to_dict()
                stubs.append(stubs_item)

        stub_overflow: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.stub_overflow, Unset):
            stub_overflow = []
            for stub_overflow_item_data in self.stub_overflow:
                stub_overflow_item = stub_overflow_item_data.to_dict()
                stub_overflow.append(stub_overflow_item)

        workers: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.workers, Unset):
            workers = []
            for workers_item_data in self.workers:
                workers_item = workers_item_data.to_dict()
                workers.append(workers_item)

        gates: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.gates, Unset):
            gates = []
            for gates_item_data in self.gates:
                gates_item = gates_item_data.to_dict()
                gates.append(gates_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "layout_version": layout_version,
            }
        )
        if nodes is not UNSET:
            field_dict["nodes"] = nodes
        if edges is not UNSET:
            field_dict["edges"] = edges
        if stubs is not UNSET:
            field_dict["stubs"] = stubs
        if stub_overflow is not UNSET:
            field_dict["stub_overflow"] = stub_overflow
        if workers is not UNSET:
            field_dict["workers"] = workers
        if gates is not UNSET:
            field_dict["gates"] = gates

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.graph_gate import GraphGate
        from ..models.layout_edge import LayoutEdge
        from ..models.layout_node import LayoutNode
        from ..models.layout_stub import LayoutStub
        from ..models.layout_worker import LayoutWorker
        from ..models.stub_overflow import StubOverflow

        d = dict(src_dict)
        layout_version = d.pop("layout_version")

        _nodes = d.pop("nodes", UNSET)
        nodes: list[LayoutNode] | Unset = UNSET
        if _nodes is not UNSET:
            nodes = []
            for nodes_item_data in _nodes:
                nodes_item = LayoutNode.from_dict(nodes_item_data)

                nodes.append(nodes_item)

        _edges = d.pop("edges", UNSET)
        edges: list[LayoutEdge] | Unset = UNSET
        if _edges is not UNSET:
            edges = []
            for edges_item_data in _edges:
                edges_item = LayoutEdge.from_dict(edges_item_data)

                edges.append(edges_item)

        _stubs = d.pop("stubs", UNSET)
        stubs: list[LayoutStub] | Unset = UNSET
        if _stubs is not UNSET:
            stubs = []
            for stubs_item_data in _stubs:
                stubs_item = LayoutStub.from_dict(stubs_item_data)

                stubs.append(stubs_item)

        _stub_overflow = d.pop("stub_overflow", UNSET)
        stub_overflow: list[StubOverflow] | Unset = UNSET
        if _stub_overflow is not UNSET:
            stub_overflow = []
            for stub_overflow_item_data in _stub_overflow:
                stub_overflow_item = StubOverflow.from_dict(stub_overflow_item_data)

                stub_overflow.append(stub_overflow_item)

        _workers = d.pop("workers", UNSET)
        workers: list[LayoutWorker] | Unset = UNSET
        if _workers is not UNSET:
            workers = []
            for workers_item_data in _workers:
                workers_item = LayoutWorker.from_dict(workers_item_data)

                workers.append(workers_item)

        _gates = d.pop("gates", UNSET)
        gates: list[GraphGate] | Unset = UNSET
        if _gates is not UNSET:
            gates = []
            for gates_item_data in _gates:
                gates_item = GraphGate.from_dict(gates_item_data)

                gates.append(gates_item)

        tiles_response = cls(
            layout_version=layout_version,
            nodes=nodes,
            edges=edges,
            stubs=stubs,
            stub_overflow=stub_overflow,
            workers=workers,
            gates=gates,
        )

        tiles_response.additional_properties = d
        return tiles_response

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
