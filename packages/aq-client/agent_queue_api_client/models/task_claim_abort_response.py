from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.task_claim_abort_response_outcome_type_0 import TaskClaimAbortResponseOutcomeType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskClaimAbortResponse")


@_attrs_define
class TaskClaimAbortResponse:
    """Exact preparation cancellation, including cleanup still in progress.

    Attributes:
        success (bool):
        outcome (None | TaskClaimAbortResponseOutcomeType0 | Unset):
        task_id (None | str | Unset):
        session_id (None | str | Unset):
        claim_epoch (int | None | Unset):
        claim_phase (None | str | Unset):
        result (None | str | Unset):
        error (None | str | Unset):
    """

    success: bool
    outcome: None | TaskClaimAbortResponseOutcomeType0 | Unset = UNSET
    task_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    claim_phase: None | str | Unset = UNSET
    result: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        outcome: None | str | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        elif isinstance(self.outcome, TaskClaimAbortResponseOutcomeType0):
            outcome = self.outcome.value
        else:
            outcome = self.outcome

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        session_id: None | str | Unset
        if isinstance(self.session_id, Unset):
            session_id = UNSET
        else:
            session_id = self.session_id

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        claim_phase: None | str | Unset
        if isinstance(self.claim_phase, Unset):
            claim_phase = UNSET
        else:
            claim_phase = self.claim_phase

        result: None | str | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        else:
            result = self.result

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
            }
        )
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if claim_phase is not UNSET:
            field_dict["claim_phase"] = claim_phase
        if result is not UNSET:
            field_dict["result"] = result
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success")

        def _parse_outcome(data: object) -> None | TaskClaimAbortResponseOutcomeType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                outcome_type_0 = TaskClaimAbortResponseOutcomeType0(data)

                return outcome_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskClaimAbortResponseOutcomeType0 | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_session_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_id = _parse_session_id(d.pop("session_id", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        def _parse_claim_phase(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        claim_phase = _parse_claim_phase(d.pop("claim_phase", UNSET))

        def _parse_result(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        result = _parse_result(d.pop("result", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        task_claim_abort_response = cls(
            success=success,
            outcome=outcome,
            task_id=task_id,
            session_id=session_id,
            claim_epoch=claim_epoch,
            claim_phase=claim_phase,
            result=result,
            error=error,
        )

        task_claim_abort_response.additional_properties = d
        return task_claim_abort_response

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
