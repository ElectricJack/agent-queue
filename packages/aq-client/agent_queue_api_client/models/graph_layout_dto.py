from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..models.graph_layout_dto_direction import GraphLayoutDTODirection
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.graph_layout_dto_cluster_bounds import GraphLayoutDTOClusterBounds
    from ..models.graph_layout_dto_grid_positions import GraphLayoutDTOGridPositions


T = TypeVar("T", bound="GraphLayoutDTO")


@_attrs_define
class GraphLayoutDTO:
    """
    Attributes:
        direction (GraphLayoutDTODirection | Unset):  Default: GraphLayoutDTODirection.TD.
        grid_positions (GraphLayoutDTOGridPositions | Unset):
        cluster_bounds (GraphLayoutDTOClusterBounds | Unset):
    """

    direction: GraphLayoutDTODirection | Unset = GraphLayoutDTODirection.TD
    grid_positions: GraphLayoutDTOGridPositions | Unset = UNSET
    cluster_bounds: GraphLayoutDTOClusterBounds | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        direction: str | Unset = UNSET
        if not isinstance(self.direction, Unset):
            direction = self.direction.value

        grid_positions: dict[str, Any] | Unset = UNSET
        if not isinstance(self.grid_positions, Unset):
            grid_positions = self.grid_positions.to_dict()

        cluster_bounds: dict[str, Any] | Unset = UNSET
        if not isinstance(self.cluster_bounds, Unset):
            cluster_bounds = self.cluster_bounds.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if direction is not UNSET:
            field_dict["direction"] = direction
        if grid_positions is not UNSET:
            field_dict["grid_positions"] = grid_positions
        if cluster_bounds is not UNSET:
            field_dict["cluster_bounds"] = cluster_bounds

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.graph_layout_dto_cluster_bounds import GraphLayoutDTOClusterBounds
        from ..models.graph_layout_dto_grid_positions import GraphLayoutDTOGridPositions

        d = dict(src_dict)
        _direction = d.pop("direction", UNSET)
        direction: GraphLayoutDTODirection | Unset
        if isinstance(_direction, Unset):
            direction = UNSET
        else:
            direction = GraphLayoutDTODirection(_direction)

        _grid_positions = d.pop("grid_positions", UNSET)
        grid_positions: GraphLayoutDTOGridPositions | Unset
        if isinstance(_grid_positions, Unset):
            grid_positions = UNSET
        else:
            grid_positions = GraphLayoutDTOGridPositions.from_dict(_grid_positions)

        _cluster_bounds = d.pop("cluster_bounds", UNSET)
        cluster_bounds: GraphLayoutDTOClusterBounds | Unset
        if isinstance(_cluster_bounds, Unset):
            cluster_bounds = UNSET
        else:
            cluster_bounds = GraphLayoutDTOClusterBounds.from_dict(_cluster_bounds)

        graph_layout_dto = cls(
            direction=direction,
            grid_positions=grid_positions,
            cluster_bounds=cluster_bounds,
        )

        return graph_layout_dto
