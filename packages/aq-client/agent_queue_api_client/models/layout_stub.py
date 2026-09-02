from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="LayoutStub")


@_attrs_define
class LayoutStub:
    """
    Attributes:
        id (str):
        project_id (str):
        x (float):
        y (float):
        w (float):
        h (float):
        title (str | Unset):  Default: ''.
    """

    id: str
    project_id: str
    x: float
    y: float
    w: float
    h: float
    title: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        project_id = self.project_id

        x = self.x

        y = self.y

        w = self.w

        h = self.h

        title = self.title

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "project_id": project_id,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
            }
        )
        if title is not UNSET:
            field_dict["title"] = title

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        project_id = d.pop("project_id")

        x = d.pop("x")

        y = d.pop("y")

        w = d.pop("w")

        h = d.pop("h")

        title = d.pop("title", UNSET)

        layout_stub = cls(
            id=id,
            project_id=project_id,
            x=x,
            y=y,
            w=w,
            h=h,
            title=title,
        )

        layout_stub.additional_properties = d
        return layout_stub

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
