from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

T = TypeVar("T", bound="SourceScan")


@_attrs_define
class SourceScan:
    """
    Attributes:
        modules (list[str]):
        triggers (list[str]):
    """

    modules: list[str]
    triggers: list[str]

    def to_dict(self) -> dict[str, Any]:
        modules = self.modules

        triggers = self.triggers

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "modules": modules,
                "triggers": triggers,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        modules = cast(list[str], d.pop("modules"))

        triggers = cast(list[str], d.pop("triggers"))

        source_scan = cls(
            modules=modules,
            triggers=triggers,
        )

        return source_scan
