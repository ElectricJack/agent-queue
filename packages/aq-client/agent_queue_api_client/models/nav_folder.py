from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="NavFolder")


@_attrs_define
class NavFolder:
    """
    Attributes:
        id (str):
        name (str):
        collapsed (bool | Unset):  Default: False.
    """

    id: str
    name: str
    collapsed: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        collapsed = self.collapsed

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "id": id,
                "name": name,
            }
        )
        if collapsed is not UNSET:
            field_dict["collapsed"] = collapsed

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        collapsed = d.pop("collapsed", UNSET)

        nav_folder = cls(
            id=id,
            name=name,
            collapsed=collapsed,
        )

        return nav_folder
