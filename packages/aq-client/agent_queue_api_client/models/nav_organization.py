from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.nav_folder import NavFolder
    from ..models.nav_organization_assignments import NavOrganizationAssignments


T = TypeVar("T", bound="NavOrganization")


@_attrs_define
class NavOrganization:
    """
    Attributes:
        folders (list[NavFolder] | Unset):
        assignments (NavOrganizationAssignments | Unset):
        project_order (list[str] | Unset):
    """

    folders: list[NavFolder] | Unset = UNSET
    assignments: NavOrganizationAssignments | Unset = UNSET
    project_order: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        folders: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.folders, Unset):
            folders = []
            for folders_item_data in self.folders:
                folders_item = folders_item_data.to_dict()
                folders.append(folders_item)

        assignments: dict[str, Any] | Unset = UNSET
        if not isinstance(self.assignments, Unset):
            assignments = self.assignments.to_dict()

        project_order: list[str] | Unset = UNSET
        if not isinstance(self.project_order, Unset):
            project_order = self.project_order

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if folders is not UNSET:
            field_dict["folders"] = folders
        if assignments is not UNSET:
            field_dict["assignments"] = assignments
        if project_order is not UNSET:
            field_dict["project_order"] = project_order

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.nav_folder import NavFolder
        from ..models.nav_organization_assignments import NavOrganizationAssignments

        d = dict(src_dict)
        _folders = d.pop("folders", UNSET)
        folders: list[NavFolder] | Unset = UNSET
        if _folders is not UNSET:
            folders = []
            for folders_item_data in _folders:
                folders_item = NavFolder.from_dict(folders_item_data)

                folders.append(folders_item)

        _assignments = d.pop("assignments", UNSET)
        assignments: NavOrganizationAssignments | Unset
        if isinstance(_assignments, Unset):
            assignments = UNSET
        else:
            assignments = NavOrganizationAssignments.from_dict(_assignments)

        project_order = cast(list[str], d.pop("project_order", UNSET))

        nav_organization = cls(
            folders=folders,
            assignments=assignments,
            project_order=project_order,
        )

        return nav_organization
