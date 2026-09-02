from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.graph_legend_dto_edge_kinds import GraphLegendDTOEdgeKinds
    from ..models.graph_legend_dto_step_kinds import GraphLegendDTOStepKinds


T = TypeVar("T", bound="GraphLegendDTO")


@_attrs_define
class GraphLegendDTO:
    """
    Attributes:
        step_kinds (GraphLegendDTOStepKinds | Unset):
        edge_kinds (GraphLegendDTOEdgeKinds | Unset):
    """

    step_kinds: GraphLegendDTOStepKinds | Unset = UNSET
    edge_kinds: GraphLegendDTOEdgeKinds | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        step_kinds: dict[str, Any] | Unset = UNSET
        if not isinstance(self.step_kinds, Unset):
            step_kinds = self.step_kinds.to_dict()

        edge_kinds: dict[str, Any] | Unset = UNSET
        if not isinstance(self.edge_kinds, Unset):
            edge_kinds = self.edge_kinds.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if step_kinds is not UNSET:
            field_dict["step_kinds"] = step_kinds
        if edge_kinds is not UNSET:
            field_dict["edge_kinds"] = edge_kinds

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.graph_legend_dto_edge_kinds import GraphLegendDTOEdgeKinds
        from ..models.graph_legend_dto_step_kinds import GraphLegendDTOStepKinds

        d = dict(src_dict)
        _step_kinds = d.pop("step_kinds", UNSET)
        step_kinds: GraphLegendDTOStepKinds | Unset
        if isinstance(_step_kinds, Unset):
            step_kinds = UNSET
        else:
            step_kinds = GraphLegendDTOStepKinds.from_dict(_step_kinds)

        _edge_kinds = d.pop("edge_kinds", UNSET)
        edge_kinds: GraphLegendDTOEdgeKinds | Unset
        if isinstance(_edge_kinds, Unset):
            edge_kinds = UNSET
        else:
            edge_kinds = GraphLegendDTOEdgeKinds.from_dict(_edge_kinds)

        graph_legend_dto = cls(
            step_kinds=step_kinds,
            edge_kinds=edge_kinds,
        )

        return graph_legend_dto
