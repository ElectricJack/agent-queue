from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_graph_project import WaitGraphProject


T = TypeVar("T", bound="WaitGraphProjection")


@_attrs_define
class WaitGraphProjection:
    """
    Attributes:
        projects (list[WaitGraphProject] | Unset):
        total_projects (int | Unset):  Default: 0.
        offset (int | Unset):  Default: 0.
        truncated (bool | Unset):  Default: False.
    """

    projects: list[WaitGraphProject] | Unset = UNSET
    total_projects: int | Unset = 0
    offset: int | Unset = 0
    truncated: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        projects: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.projects, Unset):
            projects = []
            for projects_item_data in self.projects:
                projects_item = projects_item_data.to_dict()
                projects.append(projects_item)

        total_projects = self.total_projects

        offset = self.offset

        truncated = self.truncated

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if projects is not UNSET:
            field_dict["projects"] = projects
        if total_projects is not UNSET:
            field_dict["total_projects"] = total_projects
        if offset is not UNSET:
            field_dict["offset"] = offset
        if truncated is not UNSET:
            field_dict["truncated"] = truncated

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_graph_project import WaitGraphProject

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

        wait_graph_projection = cls(
            projects=projects,
            total_projects=total_projects,
            offset=offset,
            truncated=truncated,
        )

        wait_graph_projection.additional_properties = d
        return wait_graph_projection

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
