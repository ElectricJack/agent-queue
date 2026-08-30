from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ShowPlaybookGraphResponse")


@_attrs_define
class ShowPlaybookGraphResponse:
    """
    Attributes:
        playbook_id (str):
        format_ (str):
        graph (str):
        node_count (int | Unset):  Default: 0.
        version (int | Unset):  Default: 0.
    """

    playbook_id: str
    format_: str
    graph: str
    node_count: int | Unset = 0
    version: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        format_ = self.format_

        graph = self.graph

        node_count = self.node_count

        version = self.version

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
                "format": format_,
                "graph": graph,
            }
        )
        if node_count is not UNSET:
            field_dict["node_count"] = node_count
        if version is not UNSET:
            field_dict["version"] = version

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        format_ = d.pop("format")

        graph = d.pop("graph")

        node_count = d.pop("node_count", UNSET)

        version = d.pop("version", UNSET)

        show_playbook_graph_response = cls(
            playbook_id=playbook_id,
            format_=format_,
            graph=graph,
            node_count=node_count,
            version=version,
        )

        show_playbook_graph_response.additional_properties = d
        return show_playbook_graph_response

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
