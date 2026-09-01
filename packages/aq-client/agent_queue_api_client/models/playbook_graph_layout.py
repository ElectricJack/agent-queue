from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_graph_layout_grid_positions import PlaybookGraphLayoutGridPositions


T = TypeVar("T", bound="PlaybookGraphLayout")


@_attrs_define
class PlaybookGraphLayout:
    """
    Attributes:
        direction (str | Unset):  Default: 'TD'.
        grid_positions (PlaybookGraphLayoutGridPositions | Unset):
    """

    direction: str | Unset = "TD"
    grid_positions: PlaybookGraphLayoutGridPositions | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        direction = self.direction

        grid_positions: dict[str, Any] | Unset = UNSET
        if not isinstance(self.grid_positions, Unset):
            grid_positions = self.grid_positions.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if direction is not UNSET:
            field_dict["direction"] = direction
        if grid_positions is not UNSET:
            field_dict["grid_positions"] = grid_positions

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_graph_layout_grid_positions import PlaybookGraphLayoutGridPositions  # noqa: PLC0415

        d = dict(src_dict)
        direction = d.pop("direction", UNSET)

        _grid_positions = d.pop("grid_positions", UNSET)
        grid_positions: PlaybookGraphLayoutGridPositions | Unset
        if isinstance(_grid_positions, Unset):
            grid_positions = UNSET
        else:
            grid_positions = PlaybookGraphLayoutGridPositions.from_dict(_grid_positions)

        playbook_graph_layout = cls(
            direction=direction,
            grid_positions=grid_positions,
        )

        playbook_graph_layout.additional_properties = d
        return playbook_graph_layout

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
