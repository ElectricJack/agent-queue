from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.workspace_cutover_snapshot_owners_item import WorkspaceCutoverSnapshotOwnersItem
    from ..models.workspace_cutover_snapshot_sessions_item import WorkspaceCutoverSnapshotSessionsItem
    from ..models.workspace_cutover_snapshot_source import WorkspaceCutoverSnapshotSource
    from ..models.workspace_cutover_snapshot_workspaces_item import WorkspaceCutoverSnapshotWorkspacesItem


T = TypeVar("T", bound="WorkspaceCutoverSnapshot")


@_attrs_define
class WorkspaceCutoverSnapshot:
    """
    Attributes:
        project_id (str):
        source_id (str):
        target_id (str):
        workspaces (list[WorkspaceCutoverSnapshotWorkspacesItem]):
        sessions (list[WorkspaceCutoverSnapshotSessionsItem]):
        owners (list[WorkspaceCutoverSnapshotOwnersItem]):
        source (WorkspaceCutoverSnapshotSource):
    """

    project_id: str
    source_id: str
    target_id: str
    workspaces: list[WorkspaceCutoverSnapshotWorkspacesItem]
    sessions: list[WorkspaceCutoverSnapshotSessionsItem]
    owners: list[WorkspaceCutoverSnapshotOwnersItem]
    source: WorkspaceCutoverSnapshotSource
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        source_id = self.source_id

        target_id = self.target_id

        workspaces = []
        for workspaces_item_data in self.workspaces:
            workspaces_item = workspaces_item_data.to_dict()
            workspaces.append(workspaces_item)

        sessions = []
        for sessions_item_data in self.sessions:
            sessions_item = sessions_item_data.to_dict()
            sessions.append(sessions_item)

        owners = []
        for owners_item_data in self.owners:
            owners_item = owners_item_data.to_dict()
            owners.append(owners_item)

        source = self.source.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "source_id": source_id,
                "target_id": target_id,
                "workspaces": workspaces,
                "sessions": sessions,
                "owners": owners,
                "source": source,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.workspace_cutover_snapshot_owners_item import WorkspaceCutoverSnapshotOwnersItem
        from ..models.workspace_cutover_snapshot_sessions_item import WorkspaceCutoverSnapshotSessionsItem
        from ..models.workspace_cutover_snapshot_source import WorkspaceCutoverSnapshotSource
        from ..models.workspace_cutover_snapshot_workspaces_item import WorkspaceCutoverSnapshotWorkspacesItem

        d = dict(src_dict)
        project_id = d.pop("project_id")

        source_id = d.pop("source_id")

        target_id = d.pop("target_id")

        workspaces = []
        _workspaces = d.pop("workspaces")
        for workspaces_item_data in _workspaces:
            workspaces_item = WorkspaceCutoverSnapshotWorkspacesItem.from_dict(workspaces_item_data)

            workspaces.append(workspaces_item)

        sessions = []
        _sessions = d.pop("sessions")
        for sessions_item_data in _sessions:
            sessions_item = WorkspaceCutoverSnapshotSessionsItem.from_dict(sessions_item_data)

            sessions.append(sessions_item)

        owners = []
        _owners = d.pop("owners")
        for owners_item_data in _owners:
            owners_item = WorkspaceCutoverSnapshotOwnersItem.from_dict(owners_item_data)

            owners.append(owners_item)

        source = WorkspaceCutoverSnapshotSource.from_dict(d.pop("source"))

        workspace_cutover_snapshot = cls(
            project_id=project_id,
            source_id=source_id,
            target_id=target_id,
            workspaces=workspaces,
            sessions=sessions,
            owners=owners,
            source=source,
        )

        workspace_cutover_snapshot.additional_properties = d
        return workspace_cutover_snapshot

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
