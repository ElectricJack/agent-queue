from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EscalationCreateRequest")


@_attrs_define
class EscalationCreateRequest:
    """
    Attributes:
        project_id (str):
        source_kind (str):
        source_identity (str):
        incident_key (str):
        summary (str):
        investigation (str):
        decision_requested (str):
        severity (str):
        task_id (None | str | Unset):
        choices (list[Any] | None | Unset):
    """

    project_id: str
    source_kind: str
    source_identity: str
    incident_key: str
    summary: str
    investigation: str
    decision_requested: str
    severity: str
    task_id: None | str | Unset = UNSET
    choices: list[Any] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        source_kind = self.source_kind

        source_identity = self.source_identity

        incident_key = self.incident_key

        summary = self.summary

        investigation = self.investigation

        decision_requested = self.decision_requested

        severity = self.severity

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        choices: list[Any] | None | Unset
        if isinstance(self.choices, Unset):
            choices = UNSET
        elif isinstance(self.choices, list):
            choices = self.choices

        else:
            choices = self.choices

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "source_kind": source_kind,
                "source_identity": source_identity,
                "incident_key": incident_key,
                "summary": summary,
                "investigation": investigation,
                "decision_requested": decision_requested,
                "severity": severity,
            }
        )
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if choices is not UNSET:
            field_dict["choices"] = choices

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        source_kind = d.pop("source_kind")

        source_identity = d.pop("source_identity")

        incident_key = d.pop("incident_key")

        summary = d.pop("summary")

        investigation = d.pop("investigation")

        decision_requested = d.pop("decision_requested")

        severity = d.pop("severity")

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

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

        escalation_create_request = cls(
            project_id=project_id,
            source_kind=source_kind,
            source_identity=source_identity,
            incident_key=incident_key,
            summary=summary,
            investigation=investigation,
            decision_requested=decision_requested,
            severity=severity,
            task_id=task_id,
            choices=choices,
        )

        escalation_create_request.additional_properties = d
        return escalation_create_request

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
