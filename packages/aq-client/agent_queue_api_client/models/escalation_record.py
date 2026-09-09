from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.escalation_record_terminal_evidence_type_0 import EscalationRecordTerminalEvidenceType0


T = TypeVar("T", bound="EscalationRecord")


@_attrs_define
class EscalationRecord:
    """
    Attributes:
        id (str):
        project_id (str):
        source_kind (str):
        source_identity (str):
        incident_key (str):
        supervisor_owner (str):
        summary (str):
        investigation (str):
        decision_requested (str):
        severity (str):
        state (str):
        revision (int):
        created_at (float):
        updated_at (float):
        task_id (None | str | Unset):
        task_title (None | str | Unset):
        task_status (None | str | Unset):
        choices (list[Any] | None | Unset):
        terminal_outcome (None | str | Unset):
        terminal_evidence (EscalationRecordTerminalEvidenceType0 | None | Unset):
        terminal_at (float | None | Unset):
        delivery_statuses (list[str] | None | Unset):
        pending_delivery (bool | None | Unset):
    """

    id: str
    project_id: str
    source_kind: str
    source_identity: str
    incident_key: str
    supervisor_owner: str
    summary: str
    investigation: str
    decision_requested: str
    severity: str
    state: str
    revision: int
    created_at: float
    updated_at: float
    task_id: None | str | Unset = UNSET
    task_title: None | str | Unset = UNSET
    task_status: None | str | Unset = UNSET
    choices: list[Any] | None | Unset = UNSET
    terminal_outcome: None | str | Unset = UNSET
    terminal_evidence: EscalationRecordTerminalEvidenceType0 | None | Unset = UNSET
    terminal_at: float | None | Unset = UNSET
    delivery_statuses: list[str] | None | Unset = UNSET
    pending_delivery: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.escalation_record_terminal_evidence_type_0 import EscalationRecordTerminalEvidenceType0

        id = self.id

        project_id = self.project_id

        source_kind = self.source_kind

        source_identity = self.source_identity

        incident_key = self.incident_key

        supervisor_owner = self.supervisor_owner

        summary = self.summary

        investigation = self.investigation

        decision_requested = self.decision_requested

        severity = self.severity

        state = self.state

        revision = self.revision

        created_at = self.created_at

        updated_at = self.updated_at

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        task_title: None | str | Unset
        if isinstance(self.task_title, Unset):
            task_title = UNSET
        else:
            task_title = self.task_title

        task_status: None | str | Unset
        if isinstance(self.task_status, Unset):
            task_status = UNSET
        else:
            task_status = self.task_status

        choices: list[Any] | None | Unset
        if isinstance(self.choices, Unset):
            choices = UNSET
        elif isinstance(self.choices, list):
            choices = self.choices

        else:
            choices = self.choices

        terminal_outcome: None | str | Unset
        if isinstance(self.terminal_outcome, Unset):
            terminal_outcome = UNSET
        else:
            terminal_outcome = self.terminal_outcome

        terminal_evidence: dict[str, Any] | None | Unset
        if isinstance(self.terminal_evidence, Unset):
            terminal_evidence = UNSET
        elif isinstance(self.terminal_evidence, EscalationRecordTerminalEvidenceType0):
            terminal_evidence = self.terminal_evidence.to_dict()
        else:
            terminal_evidence = self.terminal_evidence

        terminal_at: float | None | Unset
        if isinstance(self.terminal_at, Unset):
            terminal_at = UNSET
        else:
            terminal_at = self.terminal_at

        delivery_statuses: list[str] | None | Unset
        if isinstance(self.delivery_statuses, Unset):
            delivery_statuses = UNSET
        elif isinstance(self.delivery_statuses, list):
            delivery_statuses = self.delivery_statuses

        else:
            delivery_statuses = self.delivery_statuses

        pending_delivery: bool | None | Unset
        if isinstance(self.pending_delivery, Unset):
            pending_delivery = UNSET
        else:
            pending_delivery = self.pending_delivery

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "project_id": project_id,
                "source_kind": source_kind,
                "source_identity": source_identity,
                "incident_key": incident_key,
                "supervisor_owner": supervisor_owner,
                "summary": summary,
                "investigation": investigation,
                "decision_requested": decision_requested,
                "severity": severity,
                "state": state,
                "revision": revision,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if task_title is not UNSET:
            field_dict["task_title"] = task_title
        if task_status is not UNSET:
            field_dict["task_status"] = task_status
        if choices is not UNSET:
            field_dict["choices"] = choices
        if terminal_outcome is not UNSET:
            field_dict["terminal_outcome"] = terminal_outcome
        if terminal_evidence is not UNSET:
            field_dict["terminal_evidence"] = terminal_evidence
        if terminal_at is not UNSET:
            field_dict["terminal_at"] = terminal_at
        if delivery_statuses is not UNSET:
            field_dict["delivery_statuses"] = delivery_statuses
        if pending_delivery is not UNSET:
            field_dict["pending_delivery"] = pending_delivery

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.escalation_record_terminal_evidence_type_0 import EscalationRecordTerminalEvidenceType0

        d = dict(src_dict)
        id = d.pop("id")

        project_id = d.pop("project_id")

        source_kind = d.pop("source_kind")

        source_identity = d.pop("source_identity")

        incident_key = d.pop("incident_key")

        supervisor_owner = d.pop("supervisor_owner")

        summary = d.pop("summary")

        investigation = d.pop("investigation")

        decision_requested = d.pop("decision_requested")

        severity = d.pop("severity")

        state = d.pop("state")

        revision = d.pop("revision")

        created_at = d.pop("created_at")

        updated_at = d.pop("updated_at")

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_task_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_title = _parse_task_title(d.pop("task_title", UNSET))

        def _parse_task_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_status = _parse_task_status(d.pop("task_status", UNSET))

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

        def _parse_terminal_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        terminal_outcome = _parse_terminal_outcome(d.pop("terminal_outcome", UNSET))

        def _parse_terminal_evidence(data: object) -> EscalationRecordTerminalEvidenceType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                terminal_evidence_type_0 = EscalationRecordTerminalEvidenceType0.from_dict(data)

                return terminal_evidence_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EscalationRecordTerminalEvidenceType0 | None | Unset, data)

        terminal_evidence = _parse_terminal_evidence(d.pop("terminal_evidence", UNSET))

        def _parse_terminal_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        terminal_at = _parse_terminal_at(d.pop("terminal_at", UNSET))

        def _parse_delivery_statuses(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                delivery_statuses_type_0 = cast(list[str], data)

                return delivery_statuses_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        delivery_statuses = _parse_delivery_statuses(d.pop("delivery_statuses", UNSET))

        def _parse_pending_delivery(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        pending_delivery = _parse_pending_delivery(d.pop("pending_delivery", UNSET))

        escalation_record = cls(
            id=id,
            project_id=project_id,
            source_kind=source_kind,
            source_identity=source_identity,
            incident_key=incident_key,
            supervisor_owner=supervisor_owner,
            summary=summary,
            investigation=investigation,
            decision_requested=decision_requested,
            severity=severity,
            state=state,
            revision=revision,
            created_at=created_at,
            updated_at=updated_at,
            task_id=task_id,
            task_title=task_title,
            task_status=task_status,
            choices=choices,
            terminal_outcome=terminal_outcome,
            terminal_evidence=terminal_evidence,
            terminal_at=terminal_at,
            delivery_statuses=delivery_statuses,
            pending_delivery=pending_delivery,
        )

        escalation_record.additional_properties = d
        return escalation_record

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
