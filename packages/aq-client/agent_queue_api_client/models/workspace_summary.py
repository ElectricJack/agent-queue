from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.workspace_owner import WorkspaceOwner


T = TypeVar("T", bound="WorkspaceSummary")


@_attrs_define
class WorkspaceSummary:
    """
    Attributes:
        id (str):
        project_id (str):
        workspace_path (str):
        source_type (str | Unset):  Default: ''.
        name (None | str | Unset):
        kind_id (None | str | Unset):
        locked_by_agent_id (None | str | Unset):
        locked_by_task_id (None | str | Unset):
        lock_mode (None | str | Unset):
        enabled (bool | Unset):  Default: True.
        role (None | str | Unset):
        slot_index (int | None | Unset):
        mode (None | str | Unset):
        branch (None | str | Unset):
        dirty (bool | None | Unset):
        presence (None | str | Unset):
        git_status (None | str | Unset):
        git_error (None | str | Unset):
        beyond_cap (bool | None | Unset):
        owner (None | Unset | WorkspaceOwner):
    """

    id: str
    project_id: str
    workspace_path: str
    source_type: str | Unset = ""
    name: None | str | Unset = UNSET
    kind_id: None | str | Unset = UNSET
    locked_by_agent_id: None | str | Unset = UNSET
    locked_by_task_id: None | str | Unset = UNSET
    lock_mode: None | str | Unset = UNSET
    enabled: bool | Unset = True
    role: None | str | Unset = UNSET
    slot_index: int | None | Unset = UNSET
    mode: None | str | Unset = UNSET
    branch: None | str | Unset = UNSET
    dirty: bool | None | Unset = UNSET
    presence: None | str | Unset = UNSET
    git_status: None | str | Unset = UNSET
    git_error: None | str | Unset = UNSET
    beyond_cap: bool | None | Unset = UNSET
    owner: None | Unset | WorkspaceOwner = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.workspace_owner import WorkspaceOwner

        id = self.id

        project_id = self.project_id

        workspace_path = self.workspace_path

        source_type = self.source_type

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        kind_id: None | str | Unset
        if isinstance(self.kind_id, Unset):
            kind_id = UNSET
        else:
            kind_id = self.kind_id

        locked_by_agent_id: None | str | Unset
        if isinstance(self.locked_by_agent_id, Unset):
            locked_by_agent_id = UNSET
        else:
            locked_by_agent_id = self.locked_by_agent_id

        locked_by_task_id: None | str | Unset
        if isinstance(self.locked_by_task_id, Unset):
            locked_by_task_id = UNSET
        else:
            locked_by_task_id = self.locked_by_task_id

        lock_mode: None | str | Unset
        if isinstance(self.lock_mode, Unset):
            lock_mode = UNSET
        else:
            lock_mode = self.lock_mode

        enabled = self.enabled

        role: None | str | Unset
        if isinstance(self.role, Unset):
            role = UNSET
        else:
            role = self.role

        slot_index: int | None | Unset
        if isinstance(self.slot_index, Unset):
            slot_index = UNSET
        else:
            slot_index = self.slot_index

        mode: None | str | Unset
        if isinstance(self.mode, Unset):
            mode = UNSET
        else:
            mode = self.mode

        branch: None | str | Unset
        if isinstance(self.branch, Unset):
            branch = UNSET
        else:
            branch = self.branch

        dirty: bool | None | Unset
        if isinstance(self.dirty, Unset):
            dirty = UNSET
        else:
            dirty = self.dirty

        presence: None | str | Unset
        if isinstance(self.presence, Unset):
            presence = UNSET
        else:
            presence = self.presence

        git_status: None | str | Unset
        if isinstance(self.git_status, Unset):
            git_status = UNSET
        else:
            git_status = self.git_status

        git_error: None | str | Unset
        if isinstance(self.git_error, Unset):
            git_error = UNSET
        else:
            git_error = self.git_error

        beyond_cap: bool | None | Unset
        if isinstance(self.beyond_cap, Unset):
            beyond_cap = UNSET
        else:
            beyond_cap = self.beyond_cap

        owner: dict[str, Any] | None | Unset
        if isinstance(self.owner, Unset):
            owner = UNSET
        elif isinstance(self.owner, WorkspaceOwner):
            owner = self.owner.to_dict()
        else:
            owner = self.owner

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "project_id": project_id,
                "workspace_path": workspace_path,
            }
        )
        if source_type is not UNSET:
            field_dict["source_type"] = source_type
        if name is not UNSET:
            field_dict["name"] = name
        if kind_id is not UNSET:
            field_dict["kind_id"] = kind_id
        if locked_by_agent_id is not UNSET:
            field_dict["locked_by_agent_id"] = locked_by_agent_id
        if locked_by_task_id is not UNSET:
            field_dict["locked_by_task_id"] = locked_by_task_id
        if lock_mode is not UNSET:
            field_dict["lock_mode"] = lock_mode
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if role is not UNSET:
            field_dict["role"] = role
        if slot_index is not UNSET:
            field_dict["slot_index"] = slot_index
        if mode is not UNSET:
            field_dict["mode"] = mode
        if branch is not UNSET:
            field_dict["branch"] = branch
        if dirty is not UNSET:
            field_dict["dirty"] = dirty
        if presence is not UNSET:
            field_dict["presence"] = presence
        if git_status is not UNSET:
            field_dict["git_status"] = git_status
        if git_error is not UNSET:
            field_dict["git_error"] = git_error
        if beyond_cap is not UNSET:
            field_dict["beyond_cap"] = beyond_cap
        if owner is not UNSET:
            field_dict["owner"] = owner

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.workspace_owner import WorkspaceOwner

        d = dict(src_dict)
        id = d.pop("id")

        project_id = d.pop("project_id")

        workspace_path = d.pop("workspace_path")

        source_type = d.pop("source_type", UNSET)

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_kind_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        kind_id = _parse_kind_id(d.pop("kind_id", UNSET))

        def _parse_locked_by_agent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        locked_by_agent_id = _parse_locked_by_agent_id(d.pop("locked_by_agent_id", UNSET))

        def _parse_locked_by_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        locked_by_task_id = _parse_locked_by_task_id(d.pop("locked_by_task_id", UNSET))

        def _parse_lock_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        lock_mode = _parse_lock_mode(d.pop("lock_mode", UNSET))

        enabled = d.pop("enabled", UNSET)

        def _parse_role(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        role = _parse_role(d.pop("role", UNSET))

        def _parse_slot_index(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        slot_index = _parse_slot_index(d.pop("slot_index", UNSET))

        def _parse_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        mode = _parse_mode(d.pop("mode", UNSET))

        def _parse_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        branch = _parse_branch(d.pop("branch", UNSET))

        def _parse_dirty(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        dirty = _parse_dirty(d.pop("dirty", UNSET))

        def _parse_presence(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        presence = _parse_presence(d.pop("presence", UNSET))

        def _parse_git_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        git_status = _parse_git_status(d.pop("git_status", UNSET))

        def _parse_git_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        git_error = _parse_git_error(d.pop("git_error", UNSET))

        def _parse_beyond_cap(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        beyond_cap = _parse_beyond_cap(d.pop("beyond_cap", UNSET))

        def _parse_owner(data: object) -> None | Unset | WorkspaceOwner:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                owner_type_0 = WorkspaceOwner.from_dict(data)

                return owner_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WorkspaceOwner, data)

        owner = _parse_owner(d.pop("owner", UNSET))

        workspace_summary = cls(
            id=id,
            project_id=project_id,
            workspace_path=workspace_path,
            source_type=source_type,
            name=name,
            kind_id=kind_id,
            locked_by_agent_id=locked_by_agent_id,
            locked_by_task_id=locked_by_task_id,
            lock_mode=lock_mode,
            enabled=enabled,
            role=role,
            slot_index=slot_index,
            mode=mode,
            branch=branch,
            dirty=dirty,
            presence=presence,
            git_status=git_status,
            git_error=git_error,
            beyond_cap=beyond_cap,
            owner=owner,
        )

        workspace_summary.additional_properties = d
        return workspace_summary

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
