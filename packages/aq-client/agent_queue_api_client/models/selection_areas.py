from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.area import Area


T = TypeVar("T", bound="SelectionAreas")


@_attrs_define
class SelectionAreas:
    """
    Attributes:
        areas (list[Area]):
        version (Literal[1] | Unset):  Default: 1.
    """

    areas: list[Area]
    version: Literal[1] | Unset = 1

    def to_dict(self) -> dict[str, Any]:
        areas = []
        for areas_item_data in self.areas:
            areas_item = areas_item_data.to_dict()
            areas.append(areas_item)

        version = self.version

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "areas": areas,
            }
        )
        if version is not UNSET:
            field_dict["version"] = version

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.area import Area

        d = dict(src_dict)
        areas = []
        _areas = d.pop("areas")
        for areas_item_data in _areas:
            areas_item = Area.from_dict(areas_item_data)

            areas.append(areas_item)

        version = cast(Literal[1] | Unset, d.pop("version", UNSET))
        if version != 1 and not isinstance(version, Unset):
            raise ValueError(f"version must match const 1, got '{version}'")

        selection_areas = cls(
            areas=areas,
            version=version,
        )

        return selection_areas
