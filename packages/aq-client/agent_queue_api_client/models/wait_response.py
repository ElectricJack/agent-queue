from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_wait_record import AgentWaitRecord
    from ..models.wait_response_condition_type_0 import WaitResponseConditionType0
    from ..models.wait_response_decision_type_0 import WaitResponseDecisionType0
    from ..models.wait_response_evidence_type_0 import WaitResponseEvidenceType0
    from ..models.wait_response_wait_type_1 import WaitResponseWaitType1


T = TypeVar("T", bound="WaitResponse")


@_attrs_define
class WaitResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        wait (AgentWaitRecord | None | Unset | WaitResponseWaitType1):
        wait_id (None | str | Unset):
        condition (None | Unset | WaitResponseConditionType0):
        evidence (None | Unset | WaitResponseEvidenceType0):
        decision (None | Unset | WaitResponseDecisionType0):
        escalate_at (float | None | Unset):
        ceiling_at (float | None | Unset):
        continuation_id (None | str | Unset):
        warnings (list[str] | Unset):
        next_step (None | str | Unset):
    """

    success: bool | Unset = True
    wait: AgentWaitRecord | None | Unset | WaitResponseWaitType1 = UNSET
    wait_id: None | str | Unset = UNSET
    condition: None | Unset | WaitResponseConditionType0 = UNSET
    evidence: None | Unset | WaitResponseEvidenceType0 = UNSET
    decision: None | Unset | WaitResponseDecisionType0 = UNSET
    escalate_at: float | None | Unset = UNSET
    ceiling_at: float | None | Unset = UNSET
    continuation_id: None | str | Unset = UNSET
    warnings: list[str] | Unset = UNSET
    next_step: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_wait_record import AgentWaitRecord
        from ..models.wait_response_condition_type_0 import WaitResponseConditionType0
        from ..models.wait_response_decision_type_0 import WaitResponseDecisionType0
        from ..models.wait_response_evidence_type_0 import WaitResponseEvidenceType0
        from ..models.wait_response_wait_type_1 import WaitResponseWaitType1

        success = self.success

        wait: dict[str, Any] | None | Unset
        if isinstance(self.wait, Unset):
            wait = UNSET
        elif isinstance(self.wait, AgentWaitRecord):
            wait = self.wait.to_dict()
        elif isinstance(self.wait, WaitResponseWaitType1):
            wait = self.wait.to_dict()
        else:
            wait = self.wait

        wait_id: None | str | Unset
        if isinstance(self.wait_id, Unset):
            wait_id = UNSET
        else:
            wait_id = self.wait_id

        condition: dict[str, Any] | None | Unset
        if isinstance(self.condition, Unset):
            condition = UNSET
        elif isinstance(self.condition, WaitResponseConditionType0):
            condition = self.condition.to_dict()
        else:
            condition = self.condition

        evidence: dict[str, Any] | None | Unset
        if isinstance(self.evidence, Unset):
            evidence = UNSET
        elif isinstance(self.evidence, WaitResponseEvidenceType0):
            evidence = self.evidence.to_dict()
        else:
            evidence = self.evidence

        decision: dict[str, Any] | None | Unset
        if isinstance(self.decision, Unset):
            decision = UNSET
        elif isinstance(self.decision, WaitResponseDecisionType0):
            decision = self.decision.to_dict()
        else:
            decision = self.decision

        escalate_at: float | None | Unset
        if isinstance(self.escalate_at, Unset):
            escalate_at = UNSET
        else:
            escalate_at = self.escalate_at

        ceiling_at: float | None | Unset
        if isinstance(self.ceiling_at, Unset):
            ceiling_at = UNSET
        else:
            ceiling_at = self.ceiling_at

        continuation_id: None | str | Unset
        if isinstance(self.continuation_id, Unset):
            continuation_id = UNSET
        else:
            continuation_id = self.continuation_id

        warnings: list[str] | Unset = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = self.warnings

        next_step: None | str | Unset
        if isinstance(self.next_step, Unset):
            next_step = UNSET
        else:
            next_step = self.next_step

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if wait is not UNSET:
            field_dict["wait"] = wait
        if wait_id is not UNSET:
            field_dict["wait_id"] = wait_id
        if condition is not UNSET:
            field_dict["condition"] = condition
        if evidence is not UNSET:
            field_dict["evidence"] = evidence
        if decision is not UNSET:
            field_dict["decision"] = decision
        if escalate_at is not UNSET:
            field_dict["escalate_at"] = escalate_at
        if ceiling_at is not UNSET:
            field_dict["ceiling_at"] = ceiling_at
        if continuation_id is not UNSET:
            field_dict["continuation_id"] = continuation_id
        if warnings is not UNSET:
            field_dict["warnings"] = warnings
        if next_step is not UNSET:
            field_dict["next_step"] = next_step

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_wait_record import AgentWaitRecord
        from ..models.wait_response_condition_type_0 import WaitResponseConditionType0
        from ..models.wait_response_decision_type_0 import WaitResponseDecisionType0
        from ..models.wait_response_evidence_type_0 import WaitResponseEvidenceType0
        from ..models.wait_response_wait_type_1 import WaitResponseWaitType1

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_wait(data: object) -> AgentWaitRecord | None | Unset | WaitResponseWaitType1:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                wait_type_0 = AgentWaitRecord.from_dict(data)

                return wait_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                wait_type_1 = WaitResponseWaitType1.from_dict(data)

                return wait_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentWaitRecord | None | Unset | WaitResponseWaitType1, data)

        wait = _parse_wait(d.pop("wait", UNSET))

        def _parse_wait_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        wait_id = _parse_wait_id(d.pop("wait_id", UNSET))

        def _parse_condition(data: object) -> None | Unset | WaitResponseConditionType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                condition_type_0 = WaitResponseConditionType0.from_dict(data)

                return condition_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitResponseConditionType0, data)

        condition = _parse_condition(d.pop("condition", UNSET))

        def _parse_evidence(data: object) -> None | Unset | WaitResponseEvidenceType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                evidence_type_0 = WaitResponseEvidenceType0.from_dict(data)

                return evidence_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitResponseEvidenceType0, data)

        evidence = _parse_evidence(d.pop("evidence", UNSET))

        def _parse_decision(data: object) -> None | Unset | WaitResponseDecisionType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                decision_type_0 = WaitResponseDecisionType0.from_dict(data)

                return decision_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitResponseDecisionType0, data)

        decision = _parse_decision(d.pop("decision", UNSET))

        def _parse_escalate_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        escalate_at = _parse_escalate_at(d.pop("escalate_at", UNSET))

        def _parse_ceiling_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        ceiling_at = _parse_ceiling_at(d.pop("ceiling_at", UNSET))

        def _parse_continuation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        continuation_id = _parse_continuation_id(d.pop("continuation_id", UNSET))

        warnings = cast(list[str], d.pop("warnings", UNSET))

        def _parse_next_step(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_step = _parse_next_step(d.pop("next_step", UNSET))

        wait_response = cls(
            success=success,
            wait=wait,
            wait_id=wait_id,
            condition=condition,
            evidence=evidence,
            decision=decision,
            escalate_at=escalate_at,
            ceiling_at=ceiling_at,
            continuation_id=continuation_id,
            warnings=warnings,
            next_step=next_step,
        )

        return wait_response
