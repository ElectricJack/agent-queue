from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_source_record_context import AskSourceRecordContext


T = TypeVar("T", bound="AskSourceRecord")


@_attrs_define
class AskSourceRecord:
    """
    Attributes:
        decision_generation (int):
        source_fingerprint (str):
        feedback_version (int):
        id (str):
        project_id (str):
        source_kind (str):
        source_key (str):
        severity (str):
        next_actor (str):
        display_version (int):
        created_at (float):
        updated_at (float):
        context_state (str):
        context (AskSourceRecordContext):
        application_state (str):
        decision_state (str):
        answer_pending (bool | Unset):  Default: False.
        held_while_paused (bool | Unset):  Default: False.
    """

    decision_generation: int
    source_fingerprint: str
    feedback_version: int
    id: str
    project_id: str
    source_kind: str
    source_key: str
    severity: str
    next_actor: str
    display_version: int
    created_at: float
    updated_at: float
    context_state: str
    context: AskSourceRecordContext
    application_state: str
    decision_state: str
    answer_pending: bool | Unset = False
    held_while_paused: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        decision_generation = self.decision_generation

        source_fingerprint = self.source_fingerprint

        feedback_version = self.feedback_version

        id = self.id

        project_id = self.project_id

        source_kind = self.source_kind

        source_key = self.source_key

        severity = self.severity

        next_actor = self.next_actor

        display_version = self.display_version

        created_at = self.created_at

        updated_at = self.updated_at

        context_state = self.context_state

        context = self.context.to_dict()

        application_state = self.application_state

        decision_state = self.decision_state

        answer_pending = self.answer_pending

        held_while_paused = self.held_while_paused

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "decision_generation": decision_generation,
                "source_fingerprint": source_fingerprint,
                "feedback_version": feedback_version,
                "id": id,
                "project_id": project_id,
                "source_kind": source_kind,
                "source_key": source_key,
                "severity": severity,
                "next_actor": next_actor,
                "display_version": display_version,
                "created_at": created_at,
                "updated_at": updated_at,
                "context_state": context_state,
                "context": context,
                "application_state": application_state,
                "decision_state": decision_state,
            }
        )
        if answer_pending is not UNSET:
            field_dict["answer_pending"] = answer_pending
        if held_while_paused is not UNSET:
            field_dict["held_while_paused"] = held_while_paused

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_source_record_context import AskSourceRecordContext

        d = dict(src_dict)
        decision_generation = d.pop("decision_generation")

        source_fingerprint = d.pop("source_fingerprint")

        feedback_version = d.pop("feedback_version")

        id = d.pop("id")

        project_id = d.pop("project_id")

        source_kind = d.pop("source_kind")

        source_key = d.pop("source_key")

        severity = d.pop("severity")

        next_actor = d.pop("next_actor")

        display_version = d.pop("display_version")

        created_at = d.pop("created_at")

        updated_at = d.pop("updated_at")

        context_state = d.pop("context_state")

        context = AskSourceRecordContext.from_dict(d.pop("context"))

        application_state = d.pop("application_state")

        decision_state = d.pop("decision_state")

        answer_pending = d.pop("answer_pending", UNSET)

        held_while_paused = d.pop("held_while_paused", UNSET)

        ask_source_record = cls(
            decision_generation=decision_generation,
            source_fingerprint=source_fingerprint,
            feedback_version=feedback_version,
            id=id,
            project_id=project_id,
            source_kind=source_kind,
            source_key=source_key,
            severity=severity,
            next_actor=next_actor,
            display_version=display_version,
            created_at=created_at,
            updated_at=updated_at,
            context_state=context_state,
            context=context,
            application_state=application_state,
            decision_state=decision_state,
            answer_pending=answer_pending,
            held_while_paused=held_while_paused,
        )

        ask_source_record.additional_properties = d
        return ask_source_record

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
