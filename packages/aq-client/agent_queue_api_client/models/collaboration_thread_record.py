from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.collaboration_thread_record_close_reason_type_0 import CollaborationThreadRecordCloseReasonType0
from ..models.collaboration_thread_record_created_by_kind import CollaborationThreadRecordCreatedByKind
from ..models.collaboration_thread_record_state import CollaborationThreadRecordState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.collaboration_member_record import CollaborationMemberRecord
    from ..models.collaboration_thread_record_final_result_type_0 import CollaborationThreadRecordFinalResultType0


T = TypeVar("T", bound="CollaborationThreadRecord")


@_attrs_define
class CollaborationThreadRecord:
    """
    Attributes:
        id (str):
        project_id (str):
        created_by_kind (CollaborationThreadRecordCreatedByKind):
        created_by_id (str):
        idempotency_key (str):
        state (CollaborationThreadRecordState):
        created_at (float):
        deadline_at (float):
        remaining_seconds (float):
        message_budget (int):
        message_count (int):
        last_seq (int):
        version (int):
        members (list[CollaborationMemberRecord]):
        goal (None | str | Unset):
        close_reason (CollaborationThreadRecordCloseReasonType0 | None | Unset):
        closed_at (float | None | Unset):
        final_result (CollaborationThreadRecordFinalResultType0 | None | Unset):
    """

    id: str
    project_id: str
    created_by_kind: CollaborationThreadRecordCreatedByKind
    created_by_id: str
    idempotency_key: str
    state: CollaborationThreadRecordState
    created_at: float
    deadline_at: float
    remaining_seconds: float
    message_budget: int
    message_count: int
    last_seq: int
    version: int
    members: list[CollaborationMemberRecord]
    goal: None | str | Unset = UNSET
    close_reason: CollaborationThreadRecordCloseReasonType0 | None | Unset = UNSET
    closed_at: float | None | Unset = UNSET
    final_result: CollaborationThreadRecordFinalResultType0 | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.collaboration_thread_record_final_result_type_0 import CollaborationThreadRecordFinalResultType0

        id = self.id

        project_id = self.project_id

        created_by_kind = self.created_by_kind.value

        created_by_id = self.created_by_id

        idempotency_key = self.idempotency_key

        state = self.state.value

        created_at = self.created_at

        deadline_at = self.deadline_at

        remaining_seconds = self.remaining_seconds

        message_budget = self.message_budget

        message_count = self.message_count

        last_seq = self.last_seq

        version = self.version

        members = []
        for members_item_data in self.members:
            members_item = members_item_data.to_dict()
            members.append(members_item)

        goal: None | str | Unset
        if isinstance(self.goal, Unset):
            goal = UNSET
        else:
            goal = self.goal

        close_reason: None | str | Unset
        if isinstance(self.close_reason, Unset):
            close_reason = UNSET
        elif isinstance(self.close_reason, CollaborationThreadRecordCloseReasonType0):
            close_reason = self.close_reason.value
        else:
            close_reason = self.close_reason

        closed_at: float | None | Unset
        if isinstance(self.closed_at, Unset):
            closed_at = UNSET
        else:
            closed_at = self.closed_at

        final_result: dict[str, Any] | None | Unset
        if isinstance(self.final_result, Unset):
            final_result = UNSET
        elif isinstance(self.final_result, CollaborationThreadRecordFinalResultType0):
            final_result = self.final_result.to_dict()
        else:
            final_result = self.final_result

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "id": id,
                "project_id": project_id,
                "created_by_kind": created_by_kind,
                "created_by_id": created_by_id,
                "idempotency_key": idempotency_key,
                "state": state,
                "created_at": created_at,
                "deadline_at": deadline_at,
                "remaining_seconds": remaining_seconds,
                "message_budget": message_budget,
                "message_count": message_count,
                "last_seq": last_seq,
                "version": version,
                "members": members,
            }
        )
        if goal is not UNSET:
            field_dict["goal"] = goal
        if close_reason is not UNSET:
            field_dict["close_reason"] = close_reason
        if closed_at is not UNSET:
            field_dict["closed_at"] = closed_at
        if final_result is not UNSET:
            field_dict["final_result"] = final_result

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.collaboration_member_record import CollaborationMemberRecord
        from ..models.collaboration_thread_record_final_result_type_0 import CollaborationThreadRecordFinalResultType0

        d = dict(src_dict)
        id = d.pop("id")

        project_id = d.pop("project_id")

        created_by_kind = CollaborationThreadRecordCreatedByKind(d.pop("created_by_kind"))

        created_by_id = d.pop("created_by_id")

        idempotency_key = d.pop("idempotency_key")

        state = CollaborationThreadRecordState(d.pop("state"))

        created_at = d.pop("created_at")

        deadline_at = d.pop("deadline_at")

        remaining_seconds = d.pop("remaining_seconds")

        message_budget = d.pop("message_budget")

        message_count = d.pop("message_count")

        last_seq = d.pop("last_seq")

        version = d.pop("version")

        members = []
        _members = d.pop("members")
        for members_item_data in _members:
            members_item = CollaborationMemberRecord.from_dict(members_item_data)

            members.append(members_item)

        def _parse_goal(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        goal = _parse_goal(d.pop("goal", UNSET))

        def _parse_close_reason(data: object) -> CollaborationThreadRecordCloseReasonType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                close_reason_type_0 = CollaborationThreadRecordCloseReasonType0(data)

                return close_reason_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CollaborationThreadRecordCloseReasonType0 | None | Unset, data)

        close_reason = _parse_close_reason(d.pop("close_reason", UNSET))

        def _parse_closed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        closed_at = _parse_closed_at(d.pop("closed_at", UNSET))

        def _parse_final_result(data: object) -> CollaborationThreadRecordFinalResultType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                final_result_type_0 = CollaborationThreadRecordFinalResultType0.from_dict(data)

                return final_result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CollaborationThreadRecordFinalResultType0 | None | Unset, data)

        final_result = _parse_final_result(d.pop("final_result", UNSET))

        collaboration_thread_record = cls(
            id=id,
            project_id=project_id,
            created_by_kind=created_by_kind,
            created_by_id=created_by_id,
            idempotency_key=idempotency_key,
            state=state,
            created_at=created_at,
            deadline_at=deadline_at,
            remaining_seconds=remaining_seconds,
            message_budget=message_budget,
            message_count=message_count,
            last_seq=last_seq,
            version=version,
            members=members,
            goal=goal,
            close_reason=close_reason,
            closed_at=closed_at,
            final_result=final_result,
        )

        return collaboration_thread_record
