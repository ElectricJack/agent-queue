from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_graph_view_manual_positions import PlaybookGraphViewManualPositions


T = TypeVar("T", bound="PlaybookGraphView")


@_attrs_define
class PlaybookGraphView:
    """
    Attributes:
        manual_positions (PlaybookGraphViewManualPositions | Unset):
    """

    manual_positions: PlaybookGraphViewManualPositions | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        manual_positions: dict[str, Any] | Unset = UNSET
        if not isinstance(self.manual_positions, Unset):
            manual_positions = self.manual_positions.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if manual_positions is not UNSET:
            field_dict["manual_positions"] = manual_positions

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_graph_view_manual_positions import PlaybookGraphViewManualPositions

        d = dict(src_dict)
        _manual_positions = d.pop("manual_positions", UNSET)
        manual_positions: PlaybookGraphViewManualPositions | Unset
        if isinstance(_manual_positions, Unset):
            manual_positions = UNSET
        else:
            manual_positions = PlaybookGraphViewManualPositions.from_dict(_manual_positions)

        playbook_graph_view = cls(
            manual_positions=manual_positions,
        )

        return playbook_graph_view
