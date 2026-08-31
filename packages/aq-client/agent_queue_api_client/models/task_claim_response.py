from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.claim_session_summary import ClaimSessionSummary
    from ..models.task_claim_response_task_type_0 import TaskClaimResponseTaskType0


T = TypeVar("T", bound="TaskClaimResponse")


@_attrs_define
class TaskClaimResponse:
    """``task_claim`` — pull-based work selection (swarm-work-model §10).

    ``task`` is the claimed task's own **row** — the scalar fields of
    ``GetTaskResponse`` plus ``claim_epoch``, without the joined
    ``depends_on`` / ``blocks`` / ``subtasks`` / ``children`` / ``context``
    / ``labels`` sections (spec §15: building those cost ~10 statements on
    every claim; ``task_show`` remains the full view).  ``None`` for every
    non-``claimed`` result code.

        Attributes:
            success (bool):
            result (str):
            task (None | TaskClaimResponseTaskType0 | Unset):
            claim_epoch (int | None | Unset):
            session (ClaimSessionSummary | Unset): The calling session's claim bookkeeping, echoed back on every
                ``task_claim`` reply.
            reason (None | str | Unset):
            error (None | str | Unset):
    """

    success: bool
    result: str
    task: None | TaskClaimResponseTaskType0 | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    session: ClaimSessionSummary | Unset = UNSET
    reason: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.task_claim_response_task_type_0 import TaskClaimResponseTaskType0  # noqa: PLC0415

        success = self.success

        result = self.result

        task: dict[str, Any] | None | Unset
        if isinstance(self.task, Unset):
            task = UNSET
        elif isinstance(self.task, TaskClaimResponseTaskType0):
            task = self.task.to_dict()
        else:
            task = self.task

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        session: dict[str, Any] | Unset = UNSET
        if not isinstance(self.session, Unset):
            session = self.session.to_dict()

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
                "result": result,
            }
        )
        if task is not UNSET:
            field_dict["task"] = task
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if session is not UNSET:
            field_dict["session"] = session
        if reason is not UNSET:
            field_dict["reason"] = reason
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.claim_session_summary import ClaimSessionSummary  # noqa: PLC0415
        from ..models.task_claim_response_task_type_0 import TaskClaimResponseTaskType0  # noqa: PLC0415

        d = dict(src_dict)
        success = d.pop("success")

        result = d.pop("result")

        def _parse_task(data: object) -> None | TaskClaimResponseTaskType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                task_type_0 = TaskClaimResponseTaskType0.from_dict(data)

                return task_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskClaimResponseTaskType0 | Unset, data)

        task = _parse_task(d.pop("task", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        _session = d.pop("session", UNSET)
        session: ClaimSessionSummary | Unset
        if isinstance(_session, Unset):
            session = UNSET
        else:
            session = ClaimSessionSummary.from_dict(_session)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        task_claim_response = cls(
            success=success,
            result=result,
            task=task,
            claim_epoch=claim_epoch,
            session=session,
            reason=reason,
            error=error,
        )

        task_claim_response.additional_properties = d
        return task_claim_response

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
