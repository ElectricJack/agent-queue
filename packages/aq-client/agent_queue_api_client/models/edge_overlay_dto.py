from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="EdgeOverlayDTO")


@_attrs_define
class EdgeOverlayDTO:
    """
    Attributes:
        edge_id (str):
        traversal_count (int | Unset):  Default: 0.
        last_traversed_at (float | None | Unset):
    """

    edge_id: str
    traversal_count: int | Unset = 0
    last_traversed_at: float | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        edge_id = self.edge_id

        traversal_count = self.traversal_count

        last_traversed_at: float | None | Unset
        if isinstance(self.last_traversed_at, Unset):
            last_traversed_at = UNSET
        else:
            last_traversed_at = self.last_traversed_at

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "edge_id": edge_id,
            }
        )
        if traversal_count is not UNSET:
            field_dict["traversal_count"] = traversal_count
        if last_traversed_at is not UNSET:
            field_dict["last_traversed_at"] = last_traversed_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        edge_id = d.pop("edge_id")

        traversal_count = d.pop("traversal_count", UNSET)

        def _parse_last_traversed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        last_traversed_at = _parse_last_traversed_at(d.pop("last_traversed_at", UNSET))

        edge_overlay_dto = cls(
            edge_id=edge_id,
            traversal_count=traversal_count,
            last_traversed_at=last_traversed_at,
        )

        return edge_overlay_dto
