from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.activation_state_dto import ActivationStateDTO
    from ..models.artifact_ref_dto import ArtifactRefDTO
    from ..models.event_group_dto import EventGroupDTO
    from ..models.graph_diagnostic_dto import GraphDiagnosticDTO
    from ..models.graph_edge_dto import GraphEdgeDTO
    from ..models.graph_layout_dto import GraphLayoutDTO
    from ..models.graph_legend_dto import GraphLegendDTO
    from ..models.graph_node_dto import GraphNodeDTO
    from ..models.rule_cluster_dto import RuleClusterDTO


T = TypeVar("T", bound="PlaybookV2GraphResponse")


@_attrs_define
class PlaybookV2GraphResponse:
    """Filtering is server-side and lossless.  ``playbook_v2_graph(event_type=...)``
    narrows ``rules``/``nodes``/``edges`` to the rules triggered by that event and
    every node reachable from them.  ``event_groups`` always lists all events, so
    the selector never depends on the current filter.

        Attributes:
            artifact (ArtifactRefDTO): Roadmap §4 ``ArtifactRef``, projected.  Identifies exactly one
                immutable artifact; every graph, diff and overlay response carries one.
            activation (ActivationStateDTO): ``enabled`` and ``health`` are independent (design spec).  A disabled
                activation still reports its computed health; ``health="disabled"`` is used
                only when there is no active artifact at all.
            success (bool | Unset):  Default: True.
            purpose (str | Unset):  Default: 'routine'.
            event_groups (list[EventGroupDTO] | Unset):
            rules (list[RuleClusterDTO] | Unset):
            nodes (list[GraphNodeDTO] | Unset):
            edges (list[GraphEdgeDTO] | Unset):
            layout (GraphLayoutDTO | Unset):
            diagnostics (list[GraphDiagnosticDTO] | Unset):
            legend (GraphLegendDTO | Unset):
    """

    artifact: ArtifactRefDTO
    activation: ActivationStateDTO
    success: bool | Unset = True
    purpose: str | Unset = "routine"
    event_groups: list[EventGroupDTO] | Unset = UNSET
    rules: list[RuleClusterDTO] | Unset = UNSET
    nodes: list[GraphNodeDTO] | Unset = UNSET
    edges: list[GraphEdgeDTO] | Unset = UNSET
    layout: GraphLayoutDTO | Unset = UNSET
    diagnostics: list[GraphDiagnosticDTO] | Unset = UNSET
    legend: GraphLegendDTO | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        artifact = self.artifact.to_dict()

        activation = self.activation.to_dict()

        success = self.success

        purpose = self.purpose

        event_groups: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.event_groups, Unset):
            event_groups = []
            for event_groups_item_data in self.event_groups:
                event_groups_item = event_groups_item_data.to_dict()
                event_groups.append(event_groups_item)

        rules: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.rules, Unset):
            rules = []
            for rules_item_data in self.rules:
                rules_item = rules_item_data.to_dict()
                rules.append(rules_item)

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

        layout: dict[str, Any] | Unset = UNSET
        if not isinstance(self.layout, Unset):
            layout = self.layout.to_dict()

        diagnostics: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.diagnostics, Unset):
            diagnostics = []
            for diagnostics_item_data in self.diagnostics:
                diagnostics_item = diagnostics_item_data.to_dict()
                diagnostics.append(diagnostics_item)

        legend: dict[str, Any] | Unset = UNSET
        if not isinstance(self.legend, Unset):
            legend = self.legend.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "artifact": artifact,
                "activation": activation,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if purpose is not UNSET:
            field_dict["purpose"] = purpose
        if event_groups is not UNSET:
            field_dict["event_groups"] = event_groups
        if rules is not UNSET:
            field_dict["rules"] = rules
        if nodes is not UNSET:
            field_dict["nodes"] = nodes
        if edges is not UNSET:
            field_dict["edges"] = edges
        if layout is not UNSET:
            field_dict["layout"] = layout
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics
        if legend is not UNSET:
            field_dict["legend"] = legend

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.activation_state_dto import ActivationStateDTO
        from ..models.artifact_ref_dto import ArtifactRefDTO
        from ..models.event_group_dto import EventGroupDTO
        from ..models.graph_diagnostic_dto import GraphDiagnosticDTO
        from ..models.graph_edge_dto import GraphEdgeDTO
        from ..models.graph_layout_dto import GraphLayoutDTO
        from ..models.graph_legend_dto import GraphLegendDTO
        from ..models.graph_node_dto import GraphNodeDTO
        from ..models.rule_cluster_dto import RuleClusterDTO

        d = dict(src_dict)
        artifact = ArtifactRefDTO.from_dict(d.pop("artifact"))

        activation = ActivationStateDTO.from_dict(d.pop("activation"))

        success = d.pop("success", UNSET)

        purpose = d.pop("purpose", UNSET)

        _event_groups = d.pop("event_groups", UNSET)
        event_groups: list[EventGroupDTO] | Unset = UNSET
        if _event_groups is not UNSET:
            event_groups = []
            for event_groups_item_data in _event_groups:
                event_groups_item = EventGroupDTO.from_dict(event_groups_item_data)

                event_groups.append(event_groups_item)

        _rules = d.pop("rules", UNSET)
        rules: list[RuleClusterDTO] | Unset = UNSET
        if _rules is not UNSET:
            rules = []
            for rules_item_data in _rules:
                rules_item = RuleClusterDTO.from_dict(rules_item_data)

                rules.append(rules_item)

        _nodes = d.pop("nodes", UNSET)
        nodes: list[GraphNodeDTO] | Unset = UNSET
        if _nodes is not UNSET:
            nodes = []
            for nodes_item_data in _nodes:
                nodes_item = GraphNodeDTO.from_dict(nodes_item_data)

                nodes.append(nodes_item)

        _edges = d.pop("edges", UNSET)
        edges: list[GraphEdgeDTO] | Unset = UNSET
        if _edges is not UNSET:
            edges = []
            for edges_item_data in _edges:
                edges_item = GraphEdgeDTO.from_dict(edges_item_data)

                edges.append(edges_item)

        _layout = d.pop("layout", UNSET)
        layout: GraphLayoutDTO | Unset
        if isinstance(_layout, Unset):
            layout = UNSET
        else:
            layout = GraphLayoutDTO.from_dict(_layout)

        _diagnostics = d.pop("diagnostics", UNSET)
        diagnostics: list[GraphDiagnosticDTO] | Unset = UNSET
        if _diagnostics is not UNSET:
            diagnostics = []
            for diagnostics_item_data in _diagnostics:
                diagnostics_item = GraphDiagnosticDTO.from_dict(diagnostics_item_data)

                diagnostics.append(diagnostics_item)

        _legend = d.pop("legend", UNSET)
        legend: GraphLegendDTO | Unset
        if isinstance(_legend, Unset):
            legend = UNSET
        else:
            legend = GraphLegendDTO.from_dict(_legend)

        playbook_v2_graph_response = cls(
            artifact=artifact,
            activation=activation,
            success=success,
            purpose=purpose,
            event_groups=event_groups,
            rules=rules,
            nodes=nodes,
            edges=edges,
            layout=layout,
            diagnostics=diagnostics,
            legend=legend,
        )

        return playbook_v2_graph_response
