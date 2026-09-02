from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="LocateHit")


@_attrs_define
class LocateHit:
    """
    Attributes:
        id (str):
        x (float):
        y (float):
        w (float):
        h (float):
        container_id (None | str | Unset):
    """

    id: str
    x: float
    y: float
    w: float
    h: float
    container_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        x = self.x

        y = self.y

        w = self.w

        h = self.h

        container_id: None | str | Unset
        if isinstance(self.container_id, Unset):
            container_id = UNSET
        else:
            container_id = self.container_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
            }
        )
        if container_id is not UNSET:
            field_dict["container_id"] = container_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        x = d.pop("x")

        y = d.pop("y")

        w = d.pop("w")

        h = d.pop("h")

        def _parse_container_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        container_id = _parse_container_id(d.pop("container_id", UNSET))

        locate_hit = cls(
            id=id,
            x=x,
            y=y,
            w=w,
            h=h,
            container_id=container_id,
        )

        locate_hit.additional_properties = d
        return locate_hit

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
