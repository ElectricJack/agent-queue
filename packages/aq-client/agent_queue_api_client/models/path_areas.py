from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="PathAreas")


@_attrs_define
class PathAreas:
    """
    Attributes:
        paths (list[str]):
        areas (list[str]):
    """

    paths: list[str]
    areas: list[str]

    def to_dict(self) -> dict[str, Any]:
        paths = self.paths

        areas = self.areas

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "paths": paths,
                "areas": areas,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        paths = cast(list[str], d.pop("paths"))

        areas = cast(list[str], d.pop("areas"))

        path_areas = cls(
            paths=paths,
            areas=areas,
        )

        return path_areas
