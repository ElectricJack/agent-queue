from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="GridPositionDTO")


@_attrs_define
class GridPositionDTO:
    """
    Attributes:
        x (int | Unset):  Default: 0.
        y (int | Unset):  Default: 0.
    """

    x: int | Unset = 0
    y: int | Unset = 0

    def to_dict(self) -> dict[str, Any]:
        x = self.x

        y = self.y

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if x is not UNSET:
            field_dict["x"] = x
        if y is not UNSET:
            field_dict["y"] = y

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        x = d.pop("x", UNSET)

        y = d.pop("y", UNSET)

        grid_position_dto = cls(
            x=x,
            y=y,
        )

        return grid_position_dto
