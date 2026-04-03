from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.find_merge_conflict_workspaces_response_conflicts_item import (
        FindMergeConflictWorkspacesResponseConflictsItem,
    )


T = TypeVar("T", bound="FindMergeConflictWorkspacesResponse")


@_attrs_define
class FindMergeConflictWorkspacesResponse:
    """
    Attributes:
        project_id (str):
        workspaces_scanned (int | Unset):  Default: 0.
        workspaces_with_conflicts (int | Unset):  Default: 0.
        conflicts (list[FindMergeConflictWorkspacesResponseConflictsItem] | Unset):
    """

    project_id: str
    workspaces_scanned: int | Unset = 0
    workspaces_with_conflicts: int | Unset = 0
    conflicts: list[FindMergeConflictWorkspacesResponseConflictsItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        workspaces_scanned = self.workspaces_scanned

        workspaces_with_conflicts = self.workspaces_with_conflicts

        conflicts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.conflicts, Unset):
            conflicts = []
            for conflicts_item_data in self.conflicts:
                conflicts_item = conflicts_item_data.to_dict()
                conflicts.append(conflicts_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if workspaces_scanned is not UNSET:
            field_dict["workspaces_scanned"] = workspaces_scanned
        if workspaces_with_conflicts is not UNSET:
            field_dict["workspaces_with_conflicts"] = workspaces_with_conflicts
        if conflicts is not UNSET:
            field_dict["conflicts"] = conflicts

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.find_merge_conflict_workspaces_response_conflicts_item import (
            FindMergeConflictWorkspacesResponseConflictsItem,
        )

        d = dict(src_dict)
        project_id = d.pop("project_id")

        workspaces_scanned = d.pop("workspaces_scanned", UNSET)

        workspaces_with_conflicts = d.pop("workspaces_with_conflicts", UNSET)

        _conflicts = d.pop("conflicts", UNSET)
        conflicts: list[FindMergeConflictWorkspacesResponseConflictsItem] | Unset = UNSET
        if _conflicts is not UNSET:
            conflicts = []
            for conflicts_item_data in _conflicts:
                conflicts_item = FindMergeConflictWorkspacesResponseConflictsItem.from_dict(conflicts_item_data)

                conflicts.append(conflicts_item)

        find_merge_conflict_workspaces_response = cls(
            project_id=project_id,
            workspaces_scanned=workspaces_scanned,
            workspaces_with_conflicts=workspaces_with_conflicts,
            conflicts=conflicts,
        )

        find_merge_conflict_workspaces_response.additional_properties = d
        return find_merge_conflict_workspaces_response

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
