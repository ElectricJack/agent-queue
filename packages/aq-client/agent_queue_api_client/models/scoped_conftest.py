from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="ScopedConftest")


@_attrs_define
class ScopedConftest:
    """
    Attributes:
        path (str):
        subtree (str):
    """

    path: str
    subtree: str

    def to_dict(self) -> dict[str, Any]:
        path = self.path

        subtree = self.subtree

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "path": path,
                "subtree": subtree,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        path = d.pop("path")

        subtree = d.pop("subtree")

        scoped_conftest = cls(
            path=path,
            subtree=subtree,
        )

        return scoped_conftest
