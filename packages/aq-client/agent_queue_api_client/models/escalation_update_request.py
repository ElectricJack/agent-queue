from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.escalation_update_request_terminal_evidence_type_0 import EscalationUpdateRequestTerminalEvidenceType0


T = TypeVar("T", bound="EscalationUpdateRequest")


@_attrs_define
class EscalationUpdateRequest:
    """
    Attributes:
        escalation_id (str):
        expected_revision (int):
        state (None | str | Unset):
        summary (None | str | Unset):
        investigation (None | str | Unset):
        decision_requested (None | str | Unset):
        choices (list[Any] | None | Unset):
        severity (None | str | Unset):
        terminal_outcome (None | str | Unset):
        terminal_evidence (EscalationUpdateRequestTerminalEvidenceType0 | None | Unset):
    """

    escalation_id: str
    expected_revision: int
    state: None | str | Unset = UNSET
    summary: None | str | Unset = UNSET
    investigation: None | str | Unset = UNSET
    decision_requested: None | str | Unset = UNSET
    choices: list[Any] | None | Unset = UNSET
    severity: None | str | Unset = UNSET
    terminal_outcome: None | str | Unset = UNSET
    terminal_evidence: EscalationUpdateRequestTerminalEvidenceType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.escalation_update_request_terminal_evidence_type_0 import (
            EscalationUpdateRequestTerminalEvidenceType0,
        )

        escalation_id = self.escalation_id

        expected_revision = self.expected_revision

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

        summary: None | str | Unset
        if isinstance(self.summary, Unset):
            summary = UNSET
        else:
            summary = self.summary

        investigation: None | str | Unset
        if isinstance(self.investigation, Unset):
            investigation = UNSET
        else:
            investigation = self.investigation

        decision_requested: None | str | Unset
        if isinstance(self.decision_requested, Unset):
            decision_requested = UNSET
        else:
            decision_requested = self.decision_requested

        choices: list[Any] | None | Unset
        if isinstance(self.choices, Unset):
            choices = UNSET
        elif isinstance(self.choices, list):
            choices = self.choices

        else:
            choices = self.choices

        severity: None | str | Unset
        if isinstance(self.severity, Unset):
            severity = UNSET
        else:
            severity = self.severity

        terminal_outcome: None | str | Unset
        if isinstance(self.terminal_outcome, Unset):
            terminal_outcome = UNSET
        else:
            terminal_outcome = self.terminal_outcome

        terminal_evidence: dict[str, Any] | None | Unset
        if isinstance(self.terminal_evidence, Unset):
            terminal_evidence = UNSET
        elif isinstance(self.terminal_evidence, EscalationUpdateRequestTerminalEvidenceType0):
            terminal_evidence = self.terminal_evidence.to_dict()
        else:
            terminal_evidence = self.terminal_evidence

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "escalation_id": escalation_id,
                "expected_revision": expected_revision,
            }
        )
        if state is not UNSET:
            field_dict["state"] = state
        if summary is not UNSET:
            field_dict["summary"] = summary
        if investigation is not UNSET:
            field_dict["investigation"] = investigation
        if decision_requested is not UNSET:
            field_dict["decision_requested"] = decision_requested
        if choices is not UNSET:
            field_dict["choices"] = choices
        if severity is not UNSET:
            field_dict["severity"] = severity
        if terminal_outcome is not UNSET:
            field_dict["terminal_outcome"] = terminal_outcome
        if terminal_evidence is not UNSET:
            field_dict["terminal_evidence"] = terminal_evidence

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.escalation_update_request_terminal_evidence_type_0 import (
            EscalationUpdateRequestTerminalEvidenceType0,
        )

        d = dict(src_dict)
        escalation_id = d.pop("escalation_id")

        expected_revision = d.pop("expected_revision")

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        def _parse_summary(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        summary = _parse_summary(d.pop("summary", UNSET))

        def _parse_investigation(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        investigation = _parse_investigation(d.pop("investigation", UNSET))

        def _parse_decision_requested(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        decision_requested = _parse_decision_requested(d.pop("decision_requested", UNSET))

        def _parse_choices(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                choices_type_0 = cast(list[Any], data)

                return choices_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        choices = _parse_choices(d.pop("choices", UNSET))

        def _parse_severity(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        severity = _parse_severity(d.pop("severity", UNSET))

        def _parse_terminal_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        terminal_outcome = _parse_terminal_outcome(d.pop("terminal_outcome", UNSET))

        def _parse_terminal_evidence(data: object) -> EscalationUpdateRequestTerminalEvidenceType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                terminal_evidence_type_0 = EscalationUpdateRequestTerminalEvidenceType0.from_dict(data)

                return terminal_evidence_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EscalationUpdateRequestTerminalEvidenceType0 | None | Unset, data)

        terminal_evidence = _parse_terminal_evidence(d.pop("terminal_evidence", UNSET))

        escalation_update_request = cls(
            escalation_id=escalation_id,
            expected_revision=expected_revision,
            state=state,
            summary=summary,
            investigation=investigation,
            decision_requested=decision_requested,
            choices=choices,
            severity=severity,
            terminal_outcome=terminal_outcome,
            terminal_evidence=terminal_evidence,
        )

        escalation_update_request.additional_properties = d
        return escalation_update_request

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
