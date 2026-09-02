from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="ShadowSourceErrorDTO")


@_attrs_define
class ShadowSourceErrorDTO:
    """
    Attributes:
        path (str):
        errors (list[str] | Unset):
    """

    path: str
    errors: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        path = self.path

        errors: list[str] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = self.errors

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "path": path,
            }
        )
        if errors is not UNSET:
            field_dict["errors"] = errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        path = d.pop("path")

        errors = cast(list[str], d.pop("errors", UNSET))

        shadow_source_error_dto = cls(
            path=path,
            errors=errors,
        )

        return shadow_source_error_dto
