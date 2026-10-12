from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="RequiredChecks")


@_attrs_define
class RequiredChecks:
    """
    Attributes:
        version (str):
        names (list[str]):
    """

    version: str
    names: list[str]

    def to_dict(self) -> dict[str, Any]:
        version = self.version

        names = self.names

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "version": version,
                "names": names,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        version = d.pop("version")

        names = cast(list[str], d.pop("names"))

        required_checks = cls(
            version=version,
            names=names,
        )

        return required_checks
