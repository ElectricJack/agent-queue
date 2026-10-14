from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_graph_response_graph import WaitGraphResponseGraph


T = TypeVar("T", bound="WaitGraphResponse")


@_attrs_define
class WaitGraphResponse:
    """
    Attributes:
        graph (WaitGraphResponseGraph):
        success (bool | Unset):  Default: True.
    """

    graph: WaitGraphResponseGraph
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        graph = self.graph.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "graph": graph,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_graph_response_graph import WaitGraphResponseGraph

        d = dict(src_dict)
        graph = WaitGraphResponseGraph.from_dict(d.pop("graph"))

        success = d.pop("success", UNSET)

        wait_graph_response = cls(
            graph=graph,
            success=success,
        )

        return wait_graph_response
