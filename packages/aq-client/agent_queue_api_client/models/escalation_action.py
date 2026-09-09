from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.escalation_action_parameters import EscalationActionParameters
    from ..models.escalation_action_result_type_0 import EscalationActionResultType0


T = TypeVar("T", bound="EscalationAction")


@_attrs_define
class EscalationAction:
    """
    Attributes:
        id (str):
        escalation_id (str):
        reply_id (str):
        idempotency_key (str):
        action_kind (str):
        target_id (str):
        parameters (EscalationActionParameters):
        executor (str):
        started_revision (int):
        status (str):
        created_at (float):
        outcome (None | str | Unset):
        result (EscalationActionResultType0 | None | Unset):
        error (None | str | Unset):
        completed_at (float | None | Unset):
    """

    id: str
    escalation_id: str
    reply_id: str
    idempotency_key: str
    action_kind: str
    target_id: str
    parameters: EscalationActionParameters
    executor: str
    started_revision: int
    status: str
    created_at: float
    outcome: None | str | Unset = UNSET
    result: EscalationActionResultType0 | None | Unset = UNSET
    error: None | str | Unset = UNSET
    completed_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.escalation_action_result_type_0 import EscalationActionResultType0

        id = self.id

        escalation_id = self.escalation_id

        reply_id = self.reply_id

        idempotency_key = self.idempotency_key

        action_kind = self.action_kind

        target_id = self.target_id

        parameters = self.parameters.to_dict()

        executor = self.executor

        started_revision = self.started_revision

        status = self.status

        created_at = self.created_at

        outcome: None | str | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        else:
            outcome = self.outcome

        result: dict[str, Any] | None | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, EscalationActionResultType0):
            result = self.result.to_dict()
        else:
            result = self.result

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        completed_at: float | None | Unset
        if isinstance(self.completed_at, Unset):
            completed_at = UNSET
        else:
            completed_at = self.completed_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "escalation_id": escalation_id,
                "reply_id": reply_id,
                "idempotency_key": idempotency_key,
                "action_kind": action_kind,
                "target_id": target_id,
                "parameters": parameters,
                "executor": executor,
                "started_revision": started_revision,
                "status": status,
                "created_at": created_at,
            }
        )
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if result is not UNSET:
            field_dict["result"] = result
        if error is not UNSET:
            field_dict["error"] = error
        if completed_at is not UNSET:
            field_dict["completed_at"] = completed_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.escalation_action_parameters import EscalationActionParameters
        from ..models.escalation_action_result_type_0 import EscalationActionResultType0

        d = dict(src_dict)
        id = d.pop("id")

        escalation_id = d.pop("escalation_id")

        reply_id = d.pop("reply_id")

        idempotency_key = d.pop("idempotency_key")

        action_kind = d.pop("action_kind")

        target_id = d.pop("target_id")

        parameters = EscalationActionParameters.from_dict(d.pop("parameters"))

        executor = d.pop("executor")

        started_revision = d.pop("started_revision")

        status = d.pop("status")

        created_at = d.pop("created_at")

        def _parse_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_result(data: object) -> EscalationActionResultType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = EscalationActionResultType0.from_dict(data)

                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EscalationActionResultType0 | None | Unset, data)

        result = _parse_result(d.pop("result", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_completed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completed_at = _parse_completed_at(d.pop("completed_at", UNSET))

        escalation_action = cls(
            id=id,
            escalation_id=escalation_id,
            reply_id=reply_id,
            idempotency_key=idempotency_key,
            action_kind=action_kind,
            target_id=target_id,
            parameters=parameters,
            executor=executor,
            started_revision=started_revision,
            status=status,
            created_at=created_at,
            outcome=outcome,
            result=result,
            error=error,
            completed_at=completed_at,
        )

        escalation_action.additional_properties = d
        return escalation_action

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
