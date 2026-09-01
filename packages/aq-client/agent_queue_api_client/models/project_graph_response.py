from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.graph_agent import GraphAgent
    from ..models.graph_edge import GraphEdge
    from ..models.graph_gate import GraphGate
    from ..models.graph_task_node import GraphTaskNode


T = TypeVar("T", bound="ProjectGraphResponse")


@_attrs_define
class ProjectGraphResponse:
    """
    Attributes:
        tasks (list[GraphTaskNode] | Unset):
        edges (list[GraphEdge] | Unset):
        gates (list[GraphGate] | Unset):
        agents (list[GraphAgent] | Unset):
    """

    tasks: list[GraphTaskNode] | Unset = UNSET
    edges: list[GraphEdge] | Unset = UNSET
    gates: list[GraphGate] | Unset = UNSET
    agents: list[GraphAgent] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        tasks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.tasks, Unset):
            tasks = []
            for tasks_item_data in self.tasks:
                tasks_item = tasks_item_data.to_dict()
                tasks.append(tasks_item)

        edges: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.edges, Unset):
            edges = []
            for edges_item_data in self.edges:
                edges_item = edges_item_data.to_dict()
                edges.append(edges_item)

        gates: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.gates, Unset):
            gates = []
            for gates_item_data in self.gates:
                gates_item = gates_item_data.to_dict()
                gates.append(gates_item)

        agents: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.agents, Unset):
            agents = []
            for agents_item_data in self.agents:
                agents_item = agents_item_data.to_dict()
                agents.append(agents_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if tasks is not UNSET:
            field_dict["tasks"] = tasks
        if edges is not UNSET:
            field_dict["edges"] = edges
        if gates is not UNSET:
            field_dict["gates"] = gates
        if agents is not UNSET:
            field_dict["agents"] = agents

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.graph_agent import GraphAgent  # noqa: PLC0415
        from ..models.graph_edge import GraphEdge  # noqa: PLC0415
        from ..models.graph_gate import GraphGate  # noqa: PLC0415
        from ..models.graph_task_node import GraphTaskNode  # noqa: PLC0415

        d = dict(src_dict)
        _tasks = d.pop("tasks", UNSET)
        tasks: list[GraphTaskNode] | Unset = UNSET
        if _tasks is not UNSET:
            tasks = []
            for tasks_item_data in _tasks:
                tasks_item = GraphTaskNode.from_dict(tasks_item_data)

                tasks.append(tasks_item)

        _edges = d.pop("edges", UNSET)
        edges: list[GraphEdge] | Unset = UNSET
        if _edges is not UNSET:
            edges = []
            for edges_item_data in _edges:
                edges_item = GraphEdge.from_dict(edges_item_data)

                edges.append(edges_item)

        _gates = d.pop("gates", UNSET)
        gates: list[GraphGate] | Unset = UNSET
        if _gates is not UNSET:
            gates = []
            for gates_item_data in _gates:
                gates_item = GraphGate.from_dict(gates_item_data)

                gates.append(gates_item)

        _agents = d.pop("agents", UNSET)
        agents: list[GraphAgent] | Unset = UNSET
        if _agents is not UNSET:
            agents = []
            for agents_item_data in _agents:
                agents_item = GraphAgent.from_dict(agents_item_data)

                agents.append(agents_item)

        project_graph_response = cls(
            tasks=tasks,
            edges=edges,
            gates=gates,
            agents=agents,
        )

        project_graph_response.additional_properties = d
        return project_graph_response

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
