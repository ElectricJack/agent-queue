from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskCloseRequest")


@_attrs_define
class TaskCloseRequest:
    """
    Attributes:
        task_id (str): Task ID
        outcome (str): Overall task outcome
        failure_class (None | str | Unset): Failure classification, when outcome is 'fail' (optional)
        work_outcome (None | str | Unset): What actually happened to the work (optional)
        commit (None | str | Unset): Commit SHA (optional)
        notes (None | str | Unset): Closing notes (optional)
        summary (None | str | Unset): Summary of what happened, for the reviewer/dashboard/vault note. Required for
            tasks whose profile has needs_workspace: true (Dv2 Phase 2 §7 close contract).
        abandon_children (bool | Unset):  Default: False.
        claim_epoch (int | None | Unset): Current claim epoch for a pool-session caller (optional — the CLI reads it
            from .aq/claim.json).
        claim_next (bool | None | Unset): After closing, immediately claim the next ready task matching this session's
            profile (pool worker loop, swarm-work-model §10).
        wait (int | None | Unset): Seconds to long-poll for the next claim when claim_next is set (optional, clamped to
            swarm.claim_wait_max).
    """

    task_id: str
    outcome: str
    failure_class: None | str | Unset = UNSET
    work_outcome: None | str | Unset = UNSET
    commit: None | str | Unset = UNSET
    notes: None | str | Unset = UNSET
    summary: None | str | Unset = UNSET
    abandon_children: bool | Unset = False
    claim_epoch: int | None | Unset = UNSET
    claim_next: bool | None | Unset = UNSET
    wait: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        outcome = self.outcome

        failure_class: None | str | Unset
        if isinstance(self.failure_class, Unset):
            failure_class = UNSET
        else:
            failure_class = self.failure_class

        work_outcome: None | str | Unset
        if isinstance(self.work_outcome, Unset):
            work_outcome = UNSET
        else:
            work_outcome = self.work_outcome

        commit: None | str | Unset
        if isinstance(self.commit, Unset):
            commit = UNSET
        else:
            commit = self.commit

        notes: None | str | Unset
        if isinstance(self.notes, Unset):
            notes = UNSET
        else:
            notes = self.notes

        summary: None | str | Unset
        if isinstance(self.summary, Unset):
            summary = UNSET
        else:
            summary = self.summary

        abandon_children = self.abandon_children

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        claim_next: bool | None | Unset
        if isinstance(self.claim_next, Unset):
            claim_next = UNSET
        else:
            claim_next = self.claim_next

        wait: int | None | Unset
        if isinstance(self.wait, Unset):
            wait = UNSET
        else:
            wait = self.wait

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "outcome": outcome,
            }
        )
        if failure_class is not UNSET:
            field_dict["failure_class"] = failure_class
        if work_outcome is not UNSET:
            field_dict["work_outcome"] = work_outcome
        if commit is not UNSET:
            field_dict["commit"] = commit
        if notes is not UNSET:
            field_dict["notes"] = notes
        if summary is not UNSET:
            field_dict["summary"] = summary
        if abandon_children is not UNSET:
            field_dict["abandon_children"] = abandon_children
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if claim_next is not UNSET:
            field_dict["claim_next"] = claim_next
        if wait is not UNSET:
            field_dict["wait"] = wait

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        outcome = d.pop("outcome")

        def _parse_failure_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        failure_class = _parse_failure_class(d.pop("failure_class", UNSET))

        def _parse_work_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        work_outcome = _parse_work_outcome(d.pop("work_outcome", UNSET))

        def _parse_commit(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        commit = _parse_commit(d.pop("commit", UNSET))

        def _parse_notes(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        notes = _parse_notes(d.pop("notes", UNSET))

        def _parse_summary(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        summary = _parse_summary(d.pop("summary", UNSET))

        abandon_children = d.pop("abandon_children", UNSET)

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        def _parse_claim_next(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        claim_next = _parse_claim_next(d.pop("claim_next", UNSET))

        def _parse_wait(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        wait = _parse_wait(d.pop("wait", UNSET))

        task_close_request = cls(
            task_id=task_id,
            outcome=outcome,
            failure_class=failure_class,
            work_outcome=work_outcome,
            commit=commit,
            notes=notes,
            summary=summary,
            abandon_children=abandon_children,
            claim_epoch=claim_epoch,
            claim_next=claim_next,
            wait=wait,
        )

        task_close_request.additional_properties = d
        return task_close_request

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
