from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_graph_project import WaitGraphProject
    from ..models.wait_graph_projection import WaitGraphProjection


T = TypeVar("T", bound="WaitGraphResponse")


@_attrs_define
class WaitGraphResponse:
    """
    Attributes:
        projects (list[WaitGraphProject] | Unset):
        total_projects (int | Unset):  Default: 0.
        offset (int | Unset):  Default: 0.
        truncated (bool | Unset):  Default: False.
        success (bool | Unset):  Default: True.
        graph (None | Unset | WaitGraphProjection):
    """

    projects: list[WaitGraphProject] | Unset = UNSET
    total_projects: int | Unset = 0
    offset: int | Unset = 0
    truncated: bool | Unset = False
    success: bool | Unset = True
    graph: None | Unset | WaitGraphProjection = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.wait_graph_projection import WaitGraphProjection

        projects: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.projects, Unset):
            projects = []
            for projects_item_data in self.projects:
                projects_item = projects_item_data.to_dict()
                projects.append(projects_item)

        total_projects = self.total_projects

        offset = self.offset

        truncated = self.truncated

        success = self.success

        graph: dict[str, Any] | None | Unset
        if isinstance(self.graph, Unset):
            graph = UNSET
        elif isinstance(self.graph, WaitGraphProjection):
            graph = self.graph.to_dict()
        else:
            graph = self.graph

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if projects is not UNSET:
            field_dict["projects"] = projects
        if total_projects is not UNSET:
            field_dict["total_projects"] = total_projects
        if offset is not UNSET:
            field_dict["offset"] = offset
        if truncated is not UNSET:
            field_dict["truncated"] = truncated
        if success is not UNSET:
            field_dict["success"] = success
        if graph is not UNSET:
            field_dict["graph"] = graph

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_graph_project import WaitGraphProject
        from ..models.wait_graph_projection import WaitGraphProjection

        d = dict(src_dict)
        _projects = d.pop("projects", UNSET)
        projects: list[WaitGraphProject] | Unset = UNSET
        if _projects is not UNSET:
            projects = []
            for projects_item_data in _projects:
                projects_item = WaitGraphProject.from_dict(projects_item_data)

                projects.append(projects_item)

        total_projects = d.pop("total_projects", UNSET)

        offset = d.pop("offset", UNSET)

        truncated = d.pop("truncated", UNSET)

        success = d.pop("success", UNSET)

        def _parse_graph(data: object) -> None | Unset | WaitGraphProjection:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                graph_type_0 = WaitGraphProjection.from_dict(data)

                return graph_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitGraphProjection, data)

        graph = _parse_graph(d.pop("graph", UNSET))

        wait_graph_response = cls(
            projects=projects,
            total_projects=total_projects,
            offset=offset,
            truncated=truncated,
            success=success,
            graph=graph,
        )

        return wait_graph_response
