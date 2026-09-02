from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="ClusterBoundsDTO")


@_attrs_define
class ClusterBoundsDTO:
    """Grid-unit bounding box of one rule cluster.

    Attributes:
        x (int):
        y (int):
        width (int):
        height (int):
    """

    x: int
    y: int
    width: int
    height: int

    def to_dict(self) -> dict[str, Any]:
        x = self.x

        y = self.y

        width = self.width

        height = self.height

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "x": x,
                "y": y,
                "width": width,
                "height": height,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        x = d.pop("x")

        y = d.pop("y")

        width = d.pop("width")

        height = d.pop("height")

        cluster_bounds_dto = cls(
            x=x,
            y=y,
            width=width,
            height=height,
        )

        return cluster_bounds_dto
