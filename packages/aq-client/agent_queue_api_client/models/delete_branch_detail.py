from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.delete_branch_detail_action import DeleteBranchDetailAction
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.delete_local_ref import DeleteLocalRef
    from ..models.delete_worktree import DeleteWorktree


T = TypeVar("T", bound="DeleteBranchDetail")


@_attrs_define
class DeleteBranchDetail:
    """
    Attributes:
        branch (str):
        remote_state (str):
        action (DeleteBranchDetailAction):
        task_ids (list[str] | Unset):
        repository_id (None | str | Unset):
        local_refs (list[DeleteLocalRef] | Unset):
        remote_sha (None | str | Unset):
        remote_path (None | str | Unset):
        worktrees (list[DeleteWorktree] | Unset):
    """

    branch: str
    remote_state: str
    action: DeleteBranchDetailAction
    task_ids: list[str] | Unset = UNSET
    repository_id: None | str | Unset = UNSET
    local_refs: list[DeleteLocalRef] | Unset = UNSET
    remote_sha: None | str | Unset = UNSET
    remote_path: None | str | Unset = UNSET
    worktrees: list[DeleteWorktree] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        branch = self.branch

        remote_state = self.remote_state

        action = self.action.value

        task_ids: list[str] | Unset = UNSET
        if not isinstance(self.task_ids, Unset):
            task_ids = self.task_ids

        repository_id: None | str | Unset
        if isinstance(self.repository_id, Unset):
            repository_id = UNSET
        else:
            repository_id = self.repository_id

        local_refs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.local_refs, Unset):
            local_refs = []
            for local_refs_item_data in self.local_refs:
                local_refs_item = local_refs_item_data.to_dict()
                local_refs.append(local_refs_item)

        remote_sha: None | str | Unset
        if isinstance(self.remote_sha, Unset):
            remote_sha = UNSET
        else:
            remote_sha = self.remote_sha

        remote_path: None | str | Unset
        if isinstance(self.remote_path, Unset):
            remote_path = UNSET
        else:
            remote_path = self.remote_path

        worktrees: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.worktrees, Unset):
            worktrees = []
            for worktrees_item_data in self.worktrees:
                worktrees_item = worktrees_item_data.to_dict()
                worktrees.append(worktrees_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "branch": branch,
                "remote_state": remote_state,
                "action": action,
            }
        )
        if task_ids is not UNSET:
            field_dict["task_ids"] = task_ids
        if repository_id is not UNSET:
            field_dict["repository_id"] = repository_id
        if local_refs is not UNSET:
            field_dict["local_refs"] = local_refs
        if remote_sha is not UNSET:
            field_dict["remote_sha"] = remote_sha
        if remote_path is not UNSET:
            field_dict["remote_path"] = remote_path
        if worktrees is not UNSET:
            field_dict["worktrees"] = worktrees

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.delete_local_ref import DeleteLocalRef
        from ..models.delete_worktree import DeleteWorktree

        d = dict(src_dict)
        branch = d.pop("branch")

        remote_state = d.pop("remote_state")

        action = DeleteBranchDetailAction(d.pop("action"))

        task_ids = cast(list[str], d.pop("task_ids", UNSET))

        def _parse_repository_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        repository_id = _parse_repository_id(d.pop("repository_id", UNSET))

        _local_refs = d.pop("local_refs", UNSET)
        local_refs: list[DeleteLocalRef] | Unset = UNSET
        if _local_refs is not UNSET:
            local_refs = []
            for local_refs_item_data in _local_refs:
                local_refs_item = DeleteLocalRef.from_dict(local_refs_item_data)

                local_refs.append(local_refs_item)

        def _parse_remote_sha(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        remote_sha = _parse_remote_sha(d.pop("remote_sha", UNSET))

        def _parse_remote_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        remote_path = _parse_remote_path(d.pop("remote_path", UNSET))

        _worktrees = d.pop("worktrees", UNSET)
        worktrees: list[DeleteWorktree] | Unset = UNSET
        if _worktrees is not UNSET:
            worktrees = []
            for worktrees_item_data in _worktrees:
                worktrees_item = DeleteWorktree.from_dict(worktrees_item_data)

                worktrees.append(worktrees_item)

        delete_branch_detail = cls(
            branch=branch,
            remote_state=remote_state,
            action=action,
            task_ids=task_ids,
            repository_id=repository_id,
            local_refs=local_refs,
            remote_sha=remote_sha,
            remote_path=remote_path,
            worktrees=worktrees,
        )

        delete_branch_detail.additional_properties = d
        return delete_branch_detail

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
