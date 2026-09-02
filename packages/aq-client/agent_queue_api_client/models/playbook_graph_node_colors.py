from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="PlaybookGraphNodeColors")


@_attrs_define
class PlaybookGraphNodeColors:
    """
    Attributes:
        fill (str):
        stroke (str):
        text (str):
    """

    fill: str
    stroke: str
    text: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        fill = self.fill

        stroke = self.stroke

        text = self.text

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "fill": fill,
                "stroke": stroke,
                "text": text,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        fill = d.pop("fill")

        stroke = d.pop("stroke")

        text = d.pop("text")

        playbook_graph_node_colors = cls(
            fill=fill,
            stroke=stroke,
            text=text,
        )

        playbook_graph_node_colors.additional_properties = d
        return playbook_graph_node_colors

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
