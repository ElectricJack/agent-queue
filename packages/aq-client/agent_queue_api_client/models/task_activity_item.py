from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_activity_attempt import TaskActivityAttempt


T = TypeVar("T", bound="TaskActivityItem")


@_attrs_define
class TaskActivityItem:
    """
    Attributes:
        task_id (str):
        project_id (None | str | Unset):
        title (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        priority (int | None | Unset):
        parent_task_id (None | str | Unset):
        archived (bool | Unset):  Default: False.
        created_at (float | None | Unset):
        updated_at (float | None | Unset):
        last_activity_at (float | Unset):  Default: 0.0.
        attempts (list[TaskActivityAttempt] | Unset):
        attempt_count (int | Unset):  Default: 0.
        models (list[str] | Unset):
        unattributed_attempts (int | Unset):  Default: 0.
        outcome (None | str | Unset):
        work_outcome (None | str | Unset):
        failure_class (None | str | Unset):
        completed_at (float | None | Unset):
        summary (str | Unset):  Default: ''.
        pr_url (None | str | Unset):
    """

    task_id: str
    project_id: None | str | Unset = UNSET
    title: str | Unset = ""
    status: str | Unset = ""
    priority: int | None | Unset = UNSET
    parent_task_id: None | str | Unset = UNSET
    archived: bool | Unset = False
    created_at: float | None | Unset = UNSET
    updated_at: float | None | Unset = UNSET
    last_activity_at: float | Unset = 0.0
    attempts: list[TaskActivityAttempt] | Unset = UNSET
    attempt_count: int | Unset = 0
    models: list[str] | Unset = UNSET
    unattributed_attempts: int | Unset = 0
    outcome: None | str | Unset = UNSET
    work_outcome: None | str | Unset = UNSET
    failure_class: None | str | Unset = UNSET
    completed_at: float | None | Unset = UNSET
    summary: str | Unset = ""
    pr_url: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        title = self.title

        status = self.status

        priority: int | None | Unset
        if isinstance(self.priority, Unset):
            priority = UNSET
        else:
            priority = self.priority

        parent_task_id: None | str | Unset
        if isinstance(self.parent_task_id, Unset):
            parent_task_id = UNSET
        else:
            parent_task_id = self.parent_task_id

        archived = self.archived

        created_at: float | None | Unset
        if isinstance(self.created_at, Unset):
            created_at = UNSET
        else:
            created_at = self.created_at

        updated_at: float | None | Unset
        if isinstance(self.updated_at, Unset):
            updated_at = UNSET
        else:
            updated_at = self.updated_at

        last_activity_at = self.last_activity_at

        attempts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.attempts, Unset):
            attempts = []
            for attempts_item_data in self.attempts:
                attempts_item = attempts_item_data.to_dict()
                attempts.append(attempts_item)

        attempt_count = self.attempt_count

        models: list[str] | Unset = UNSET
        if not isinstance(self.models, Unset):
            models = self.models

        unattributed_attempts = self.unattributed_attempts

        outcome: None | str | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        else:
            outcome = self.outcome

        work_outcome: None | str | Unset
        if isinstance(self.work_outcome, Unset):
            work_outcome = UNSET
        else:
            work_outcome = self.work_outcome

        failure_class: None | str | Unset
        if isinstance(self.failure_class, Unset):
            failure_class = UNSET
        else:
            failure_class = self.failure_class

        completed_at: float | None | Unset
        if isinstance(self.completed_at, Unset):
            completed_at = UNSET
        else:
            completed_at = self.completed_at

        summary = self.summary

        pr_url: None | str | Unset
        if isinstance(self.pr_url, Unset):
            pr_url = UNSET
        else:
            pr_url = self.pr_url

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if title is not UNSET:
            field_dict["title"] = title
        if status is not UNSET:
            field_dict["status"] = status
        if priority is not UNSET:
            field_dict["priority"] = priority
        if parent_task_id is not UNSET:
            field_dict["parent_task_id"] = parent_task_id
        if archived is not UNSET:
            field_dict["archived"] = archived
        if created_at is not UNSET:
            field_dict["created_at"] = created_at
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at
        if last_activity_at is not UNSET:
            field_dict["last_activity_at"] = last_activity_at
        if attempts is not UNSET:
            field_dict["attempts"] = attempts
        if attempt_count is not UNSET:
            field_dict["attempt_count"] = attempt_count
        if models is not UNSET:
            field_dict["models"] = models
        if unattributed_attempts is not UNSET:
            field_dict["unattributed_attempts"] = unattributed_attempts
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if work_outcome is not UNSET:
            field_dict["work_outcome"] = work_outcome
        if failure_class is not UNSET:
            field_dict["failure_class"] = failure_class
        if completed_at is not UNSET:
            field_dict["completed_at"] = completed_at
        if summary is not UNSET:
            field_dict["summary"] = summary
        if pr_url is not UNSET:
            field_dict["pr_url"] = pr_url

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_activity_attempt import TaskActivityAttempt

        d = dict(src_dict)
        task_id = d.pop("task_id")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        title = d.pop("title", UNSET)

        status = d.pop("status", UNSET)

        def _parse_priority(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        priority = _parse_priority(d.pop("priority", UNSET))

        def _parse_parent_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_task_id = _parse_parent_task_id(d.pop("parent_task_id", UNSET))

        archived = d.pop("archived", UNSET)

        def _parse_created_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        created_at = _parse_created_at(d.pop("created_at", UNSET))

        def _parse_updated_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        updated_at = _parse_updated_at(d.pop("updated_at", UNSET))

        last_activity_at = d.pop("last_activity_at", UNSET)

        _attempts = d.pop("attempts", UNSET)
        attempts: list[TaskActivityAttempt] | Unset = UNSET
        if _attempts is not UNSET:
            attempts = []
            for attempts_item_data in _attempts:
                attempts_item = TaskActivityAttempt.from_dict(attempts_item_data)

                attempts.append(attempts_item)

        attempt_count = d.pop("attempt_count", UNSET)

        models = cast(list[str], d.pop("models", UNSET))

        unattributed_attempts = d.pop("unattributed_attempts", UNSET)

        def _parse_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_work_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        work_outcome = _parse_work_outcome(d.pop("work_outcome", UNSET))

        def _parse_failure_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        failure_class = _parse_failure_class(d.pop("failure_class", UNSET))

        def _parse_completed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completed_at = _parse_completed_at(d.pop("completed_at", UNSET))

        summary = d.pop("summary", UNSET)

        def _parse_pr_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pr_url = _parse_pr_url(d.pop("pr_url", UNSET))

        task_activity_item = cls(
            task_id=task_id,
            project_id=project_id,
            title=title,
            status=status,
            priority=priority,
            parent_task_id=parent_task_id,
            archived=archived,
            created_at=created_at,
            updated_at=updated_at,
            last_activity_at=last_activity_at,
            attempts=attempts,
            attempt_count=attempt_count,
            models=models,
            unattributed_attempts=unattributed_attempts,
            outcome=outcome,
            work_outcome=work_outcome,
            failure_class=failure_class,
            completed_at=completed_at,
            summary=summary,
            pr_url=pr_url,
        )

        task_activity_item.additional_properties = d
        return task_activity_item

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
