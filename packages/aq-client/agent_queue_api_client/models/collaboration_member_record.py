from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.collaboration_member_record_state import CollaborationMemberRecordState
from ..types import UNSET, Unset

T = TypeVar("T", bound="CollaborationMemberRecord")


@_attrs_define
class CollaborationMemberRecord:
    """One member task; ``needs_accept`` means its live claim has not joined.

    Attributes:
        task_id (str):
        state (CollaborationMemberRecordState):
        invited_at (float):
        task_status (str):
        running (bool):
        needs_accept (bool):
        accepted_at (float | None | Unset):
        accepted_claim_epoch (int | None | Unset):
        removed_at (float | None | Unset):
        task_claim_epoch (int | None | Unset):
    """

    task_id: str
    state: CollaborationMemberRecordState
    invited_at: float
    task_status: str
    running: bool
    needs_accept: bool
    accepted_at: float | None | Unset = UNSET
    accepted_claim_epoch: int | None | Unset = UNSET
    removed_at: float | None | Unset = UNSET
    task_claim_epoch: int | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        state = self.state.value

        invited_at = self.invited_at

        task_status = self.task_status

        running = self.running

        needs_accept = self.needs_accept

        accepted_at: float | None | Unset
        if isinstance(self.accepted_at, Unset):
            accepted_at = UNSET
        else:
            accepted_at = self.accepted_at

        accepted_claim_epoch: int | None | Unset
        if isinstance(self.accepted_claim_epoch, Unset):
            accepted_claim_epoch = UNSET
        else:
            accepted_claim_epoch = self.accepted_claim_epoch

        removed_at: float | None | Unset
        if isinstance(self.removed_at, Unset):
            removed_at = UNSET
        else:
            removed_at = self.removed_at

        task_claim_epoch: int | None | Unset
        if isinstance(self.task_claim_epoch, Unset):
            task_claim_epoch = UNSET
        else:
            task_claim_epoch = self.task_claim_epoch

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "task_id": task_id,
                "state": state,
                "invited_at": invited_at,
                "task_status": task_status,
                "running": running,
                "needs_accept": needs_accept,
            }
        )
        if accepted_at is not UNSET:
            field_dict["accepted_at"] = accepted_at
        if accepted_claim_epoch is not UNSET:
            field_dict["accepted_claim_epoch"] = accepted_claim_epoch
        if removed_at is not UNSET:
            field_dict["removed_at"] = removed_at
        if task_claim_epoch is not UNSET:
            field_dict["task_claim_epoch"] = task_claim_epoch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        state = CollaborationMemberRecordState(d.pop("state"))

        invited_at = d.pop("invited_at")

        task_status = d.pop("task_status")

        running = d.pop("running")

        needs_accept = d.pop("needs_accept")

        def _parse_accepted_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        accepted_at = _parse_accepted_at(d.pop("accepted_at", UNSET))

        def _parse_accepted_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        accepted_claim_epoch = _parse_accepted_claim_epoch(d.pop("accepted_claim_epoch", UNSET))

        def _parse_removed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        removed_at = _parse_removed_at(d.pop("removed_at", UNSET))

        def _parse_task_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        task_claim_epoch = _parse_task_claim_epoch(d.pop("task_claim_epoch", UNSET))

        collaboration_member_record = cls(
            task_id=task_id,
            state=state,
            invited_at=invited_at,
            task_status=task_status,
            running=running,
            needs_accept=needs_accept,
            accepted_at=accepted_at,
            accepted_claim_epoch=accepted_claim_epoch,
            removed_at=removed_at,
            task_claim_epoch=task_claim_epoch,
        )

        return collaboration_member_record
