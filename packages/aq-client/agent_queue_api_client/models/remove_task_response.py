from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.remove_task_response_branches import RemoveTaskResponseBranches
from ..models.remove_task_response_disposition import RemoveTaskResponseDisposition
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.delete_branch_detail import DeleteBranchDetail
    from ..models.delete_cleanup_result import DeleteCleanupResult
    from ..models.delete_session import DeleteSession
    from ..models.delete_task_summary import DeleteTaskSummary
    from ..models.delete_warning import DeleteWarning


T = TypeVar("T", bound="RemoveTaskResponse")


@_attrs_define
class RemoveTaskResponse:
    """Deprecated delete alias, including the legacy removed result key.

    Attributes:
        title (str):
        disposition (RemoveTaskResponseDisposition):
        success (bool | Unset):  Default: True.
        deleted (None | str | Unset):
        task_ids (list[str] | Unset):
        recursive (bool | Unset):  Default: False.
        branches (RemoveTaskResponseBranches | Unset):  Default: RemoveTaskResponseBranches.KEEP.
        stop_running (bool | Unset):  Default: False.
        tasks (list[DeleteTaskSummary] | Unset):
        reparented_tasks (list[str] | Unset):
        sessions (list[DeleteSession] | Unset):
        dependents (list[str] | Unset):
        gate_ids (list[str] | Unset):
        review_ids (list[str] | Unset):
        escalation_ids (list[str] | Unset):
        batch_ids (list[str] | Unset):
        operation_ids (list[str] | Unset):
        branch_details (list[DeleteBranchDetail] | Unset):
        warnings (list[DeleteWarning] | Unset):
        cleanup_results (list[DeleteCleanupResult] | Unset):
        audit_id (int | None | Unset):
        reason (None | str | Unset):
        removed (None | str | Unset):
        deprecated_alias (str | Unset):  Default: 'remove_task'.
    """

    title: str
    disposition: RemoveTaskResponseDisposition
    success: bool | Unset = True
    deleted: None | str | Unset = UNSET
    task_ids: list[str] | Unset = UNSET
    recursive: bool | Unset = False
    branches: RemoveTaskResponseBranches | Unset = RemoveTaskResponseBranches.KEEP
    stop_running: bool | Unset = False
    tasks: list[DeleteTaskSummary] | Unset = UNSET
    reparented_tasks: list[str] | Unset = UNSET
    sessions: list[DeleteSession] | Unset = UNSET
    dependents: list[str] | Unset = UNSET
    gate_ids: list[str] | Unset = UNSET
    review_ids: list[str] | Unset = UNSET
    escalation_ids: list[str] | Unset = UNSET
    batch_ids: list[str] | Unset = UNSET
    operation_ids: list[str] | Unset = UNSET
    branch_details: list[DeleteBranchDetail] | Unset = UNSET
    warnings: list[DeleteWarning] | Unset = UNSET
    cleanup_results: list[DeleteCleanupResult] | Unset = UNSET
    audit_id: int | None | Unset = UNSET
    reason: None | str | Unset = UNSET
    removed: None | str | Unset = UNSET
    deprecated_alias: str | Unset = "remove_task"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        title = self.title

        disposition = self.disposition.value

        success = self.success

        deleted: None | str | Unset
        if isinstance(self.deleted, Unset):
            deleted = UNSET
        else:
            deleted = self.deleted

        task_ids: list[str] | Unset = UNSET
        if not isinstance(self.task_ids, Unset):
            task_ids = self.task_ids

        recursive = self.recursive

        branches: str | Unset = UNSET
        if not isinstance(self.branches, Unset):
            branches = self.branches.value

        stop_running = self.stop_running

        tasks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.tasks, Unset):
            tasks = []
            for tasks_item_data in self.tasks:
                tasks_item = tasks_item_data.to_dict()
                tasks.append(tasks_item)

        reparented_tasks: list[str] | Unset = UNSET
        if not isinstance(self.reparented_tasks, Unset):
            reparented_tasks = self.reparented_tasks

        sessions: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.sessions, Unset):
            sessions = []
            for sessions_item_data in self.sessions:
                sessions_item = sessions_item_data.to_dict()
                sessions.append(sessions_item)

        dependents: list[str] | Unset = UNSET
        if not isinstance(self.dependents, Unset):
            dependents = self.dependents

        gate_ids: list[str] | Unset = UNSET
        if not isinstance(self.gate_ids, Unset):
            gate_ids = self.gate_ids

        review_ids: list[str] | Unset = UNSET
        if not isinstance(self.review_ids, Unset):
            review_ids = self.review_ids

        escalation_ids: list[str] | Unset = UNSET
        if not isinstance(self.escalation_ids, Unset):
            escalation_ids = self.escalation_ids

        batch_ids: list[str] | Unset = UNSET
        if not isinstance(self.batch_ids, Unset):
            batch_ids = self.batch_ids

        operation_ids: list[str] | Unset = UNSET
        if not isinstance(self.operation_ids, Unset):
            operation_ids = self.operation_ids

        branch_details: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.branch_details, Unset):
            branch_details = []
            for branch_details_item_data in self.branch_details:
                branch_details_item = branch_details_item_data.to_dict()
                branch_details.append(branch_details_item)

        warnings: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = []
            for warnings_item_data in self.warnings:
                warnings_item = warnings_item_data.to_dict()
                warnings.append(warnings_item)

        cleanup_results: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.cleanup_results, Unset):
            cleanup_results = []
            for cleanup_results_item_data in self.cleanup_results:
                cleanup_results_item = cleanup_results_item_data.to_dict()
                cleanup_results.append(cleanup_results_item)

        audit_id: int | None | Unset
        if isinstance(self.audit_id, Unset):
            audit_id = UNSET
        else:
            audit_id = self.audit_id

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        removed: None | str | Unset
        if isinstance(self.removed, Unset):
            removed = UNSET
        else:
            removed = self.removed

        deprecated_alias = self.deprecated_alias

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "title": title,
                "disposition": disposition,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if deleted is not UNSET:
            field_dict["deleted"] = deleted
        if task_ids is not UNSET:
            field_dict["task_ids"] = task_ids
        if recursive is not UNSET:
            field_dict["recursive"] = recursive
        if branches is not UNSET:
            field_dict["branches"] = branches
        if stop_running is not UNSET:
            field_dict["stop_running"] = stop_running
        if tasks is not UNSET:
            field_dict["tasks"] = tasks
        if reparented_tasks is not UNSET:
            field_dict["reparented_tasks"] = reparented_tasks
        if sessions is not UNSET:
            field_dict["sessions"] = sessions
        if dependents is not UNSET:
            field_dict["dependents"] = dependents
        if gate_ids is not UNSET:
            field_dict["gate_ids"] = gate_ids
        if review_ids is not UNSET:
            field_dict["review_ids"] = review_ids
        if escalation_ids is not UNSET:
            field_dict["escalation_ids"] = escalation_ids
        if batch_ids is not UNSET:
            field_dict["batch_ids"] = batch_ids
        if operation_ids is not UNSET:
            field_dict["operation_ids"] = operation_ids
        if branch_details is not UNSET:
            field_dict["branch_details"] = branch_details
        if warnings is not UNSET:
            field_dict["warnings"] = warnings
        if cleanup_results is not UNSET:
            field_dict["cleanup_results"] = cleanup_results
        if audit_id is not UNSET:
            field_dict["audit_id"] = audit_id
        if reason is not UNSET:
            field_dict["reason"] = reason
        if removed is not UNSET:
            field_dict["removed"] = removed
        if deprecated_alias is not UNSET:
            field_dict["deprecated_alias"] = deprecated_alias

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.delete_branch_detail import DeleteBranchDetail
        from ..models.delete_cleanup_result import DeleteCleanupResult
        from ..models.delete_session import DeleteSession
        from ..models.delete_task_summary import DeleteTaskSummary
        from ..models.delete_warning import DeleteWarning

        d = dict(src_dict)
        title = d.pop("title")

        disposition = RemoveTaskResponseDisposition(d.pop("disposition"))

        success = d.pop("success", UNSET)

        def _parse_deleted(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        deleted = _parse_deleted(d.pop("deleted", UNSET))

        task_ids = cast(list[str], d.pop("task_ids", UNSET))

        recursive = d.pop("recursive", UNSET)

        _branches = d.pop("branches", UNSET)
        branches: RemoveTaskResponseBranches | Unset
        if isinstance(_branches, Unset):
            branches = UNSET
        else:
            branches = RemoveTaskResponseBranches(_branches)

        stop_running = d.pop("stop_running", UNSET)

        _tasks = d.pop("tasks", UNSET)
        tasks: list[DeleteTaskSummary] | Unset = UNSET
        if _tasks is not UNSET:
            tasks = []
            for tasks_item_data in _tasks:
                tasks_item = DeleteTaskSummary.from_dict(tasks_item_data)

                tasks.append(tasks_item)

        reparented_tasks = cast(list[str], d.pop("reparented_tasks", UNSET))

        _sessions = d.pop("sessions", UNSET)
        sessions: list[DeleteSession] | Unset = UNSET
        if _sessions is not UNSET:
            sessions = []
            for sessions_item_data in _sessions:
                sessions_item = DeleteSession.from_dict(sessions_item_data)

                sessions.append(sessions_item)

        dependents = cast(list[str], d.pop("dependents", UNSET))

        gate_ids = cast(list[str], d.pop("gate_ids", UNSET))

        review_ids = cast(list[str], d.pop("review_ids", UNSET))

        escalation_ids = cast(list[str], d.pop("escalation_ids", UNSET))

        batch_ids = cast(list[str], d.pop("batch_ids", UNSET))

        operation_ids = cast(list[str], d.pop("operation_ids", UNSET))

        _branch_details = d.pop("branch_details", UNSET)
        branch_details: list[DeleteBranchDetail] | Unset = UNSET
        if _branch_details is not UNSET:
            branch_details = []
            for branch_details_item_data in _branch_details:
                branch_details_item = DeleteBranchDetail.from_dict(branch_details_item_data)

                branch_details.append(branch_details_item)

        _warnings = d.pop("warnings", UNSET)
        warnings: list[DeleteWarning] | Unset = UNSET
        if _warnings is not UNSET:
            warnings = []
            for warnings_item_data in _warnings:
                warnings_item = DeleteWarning.from_dict(warnings_item_data)

                warnings.append(warnings_item)

        _cleanup_results = d.pop("cleanup_results", UNSET)
        cleanup_results: list[DeleteCleanupResult] | Unset = UNSET
        if _cleanup_results is not UNSET:
            cleanup_results = []
            for cleanup_results_item_data in _cleanup_results:
                cleanup_results_item = DeleteCleanupResult.from_dict(cleanup_results_item_data)

                cleanup_results.append(cleanup_results_item)

        def _parse_audit_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        audit_id = _parse_audit_id(d.pop("audit_id", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_removed(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        removed = _parse_removed(d.pop("removed", UNSET))

        deprecated_alias = d.pop("deprecated_alias", UNSET)

        remove_task_response = cls(
            title=title,
            disposition=disposition,
            success=success,
            deleted=deleted,
            task_ids=task_ids,
            recursive=recursive,
            branches=branches,
            stop_running=stop_running,
            tasks=tasks,
            reparented_tasks=reparented_tasks,
            sessions=sessions,
            dependents=dependents,
            gate_ids=gate_ids,
            review_ids=review_ids,
            escalation_ids=escalation_ids,
            batch_ids=batch_ids,
            operation_ids=operation_ids,
            branch_details=branch_details,
            warnings=warnings,
            cleanup_results=cleanup_results,
            audit_id=audit_id,
            reason=reason,
            removed=removed,
            deprecated_alias=deprecated_alias,
        )

        remove_task_response.additional_properties = d
        return remove_task_response

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
