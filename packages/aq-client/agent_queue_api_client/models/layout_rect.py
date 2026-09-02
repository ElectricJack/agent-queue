from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="LayoutRect")


@_attrs_define
class LayoutRect:
    """
    Attributes:
        x0 (float):
        y0 (float):
        x1 (float):
        y1 (float):
    """

    x0: float
    y0: float
    x1: float
    y1: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        x0 = self.x0

        y0 = self.y0

        x1 = self.x1

        y1 = self.y1

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        x0 = d.pop("x0")

        y0 = d.pop("y0")

        x1 = d.pop("x1")

        y1 = d.pop("y1")

        layout_rect = cls(
            x0=x0,
            y0=y0,
            x1=x1,
            y1=y1,
        )

        layout_rect.additional_properties = d
        return layout_rect

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
