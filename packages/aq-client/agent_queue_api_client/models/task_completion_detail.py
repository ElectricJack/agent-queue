from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_completion_detail_deliverables_item import TaskCompletionDetailDeliverablesItem


T = TypeVar("T", bound="TaskCompletionDetail")


@_attrs_define
class TaskCompletionDetail:
    """
    Attributes:
        id (str):
        task_id (str):
        outcome (str):
        work_outcome (None | str | Unset):
        failure_class (None | str | Unset):
        changes (str | Unset):  Default: ''.
        verification (str | Unset):  Default: ''.
        tests (list[str] | Unset):
        commands (list[str] | Unset):
        branch (None | str | Unset):
        commits (list[str] | Unset):
        pr_url (None | str | Unset):
        summary (str | Unset):  Default: ''.
        notes (str | Unset):  Default: ''.
        deliverables (list[TaskCompletionDetailDeliverablesItem] | Unset):
        completed_at (float | Unset):  Default: 0.0.
    """

    id: str
    task_id: str
    outcome: str
    work_outcome: None | str | Unset = UNSET
    failure_class: None | str | Unset = UNSET
    changes: str | Unset = ""
    verification: str | Unset = ""
    tests: list[str] | Unset = UNSET
    commands: list[str] | Unset = UNSET
    branch: None | str | Unset = UNSET
    commits: list[str] | Unset = UNSET
    pr_url: None | str | Unset = UNSET
    summary: str | Unset = ""
    notes: str | Unset = ""
    deliverables: list[TaskCompletionDetailDeliverablesItem] | Unset = UNSET
    completed_at: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        task_id = self.task_id

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

        changes = self.changes

        verification = self.verification

        tests: list[str] | Unset = UNSET
        if not isinstance(self.tests, Unset):
            tests = self.tests

        commands: list[str] | Unset = UNSET
        if not isinstance(self.commands, Unset):
            commands = self.commands

        branch: None | str | Unset
        if isinstance(self.branch, Unset):
            branch = UNSET
        else:
            branch = self.branch

        commits: list[str] | Unset = UNSET
        if not isinstance(self.commits, Unset):
            commits = self.commits

        pr_url: None | str | Unset
        if isinstance(self.pr_url, Unset):
            pr_url = UNSET
        else:
            pr_url = self.pr_url

        summary = self.summary

        notes = self.notes

        deliverables: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.deliverables, Unset):
            deliverables = []
            for deliverables_item_data in self.deliverables:
                deliverables_item = deliverables_item_data.to_dict()
                deliverables.append(deliverables_item)

        completed_at = self.completed_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "task_id": task_id,
                "outcome": outcome,
            }
        )
        if work_outcome is not UNSET:
            field_dict["work_outcome"] = work_outcome
        if failure_class is not UNSET:
            field_dict["failure_class"] = failure_class
        if changes is not UNSET:
            field_dict["changes"] = changes
        if verification is not UNSET:
            field_dict["verification"] = verification
        if tests is not UNSET:
            field_dict["tests"] = tests
        if commands is not UNSET:
            field_dict["commands"] = commands
        if branch is not UNSET:
            field_dict["branch"] = branch
        if commits is not UNSET:
            field_dict["commits"] = commits
        if pr_url is not UNSET:
            field_dict["pr_url"] = pr_url
        if summary is not UNSET:
            field_dict["summary"] = summary
        if notes is not UNSET:
            field_dict["notes"] = notes
        if deliverables is not UNSET:
            field_dict["deliverables"] = deliverables
        if completed_at is not UNSET:
            field_dict["completed_at"] = completed_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_completion_detail_deliverables_item import TaskCompletionDetailDeliverablesItem

        d = dict(src_dict)
        id = d.pop("id")

        task_id = d.pop("task_id")

        outcome = d.pop("outcome")

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

        changes = d.pop("changes", UNSET)

        verification = d.pop("verification", UNSET)

        tests = cast(list[str], d.pop("tests", UNSET))

        commands = cast(list[str], d.pop("commands", UNSET))

        def _parse_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        branch = _parse_branch(d.pop("branch", UNSET))

        commits = cast(list[str], d.pop("commits", UNSET))

        def _parse_pr_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pr_url = _parse_pr_url(d.pop("pr_url", UNSET))

        summary = d.pop("summary", UNSET)

        notes = d.pop("notes", UNSET)

        _deliverables = d.pop("deliverables", UNSET)
        deliverables: list[TaskCompletionDetailDeliverablesItem] | Unset = UNSET
        if _deliverables is not UNSET:
            deliverables = []
            for deliverables_item_data in _deliverables:
                deliverables_item = TaskCompletionDetailDeliverablesItem.from_dict(deliverables_item_data)

                deliverables.append(deliverables_item)

        completed_at = d.pop("completed_at", UNSET)

        task_completion_detail = cls(
            id=id,
            task_id=task_id,
            outcome=outcome,
            work_outcome=work_outcome,
            failure_class=failure_class,
            changes=changes,
            verification=verification,
            tests=tests,
            commands=commands,
            branch=branch,
            commits=commits,
            pr_url=pr_url,
            summary=summary,
            notes=notes,
            deliverables=deliverables,
            completed_at=completed_at,
        )

        task_completion_detail.additional_properties = d
        return task_completion_detail

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
