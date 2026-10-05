from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.operator_decision_model_effect import OperatorDecisionModelEffect
from ..models.operator_decision_model_object_kind import OperatorDecisionModelObjectKind
from ..models.operator_decision_model_source import OperatorDecisionModelSource
from ..types import UNSET, Unset

T = TypeVar("T", bound="OperatorDecisionModel")


@_attrs_define
class OperatorDecisionModel:
    """
    Attributes:
        id (str):
        project_id (str):
        object_kind (OperatorDecisionModelObjectKind):
        object_id (str):
        effect (OperatorDecisionModelEffect):
        operator (str):
        decision (str):
        source (OperatorDecisionModelSource):
        source_ref (str):
        recorded_by (str):
        created_at (float):
        idempotency_key (str):
        releases (None | str | Unset):
        active (bool | None | Unset):
    """

    id: str
    project_id: str
    object_kind: OperatorDecisionModelObjectKind
    object_id: str
    effect: OperatorDecisionModelEffect
    operator: str
    decision: str
    source: OperatorDecisionModelSource
    source_ref: str
    recorded_by: str
    created_at: float
    idempotency_key: str
    releases: None | str | Unset = UNSET
    active: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        project_id = self.project_id

        object_kind = self.object_kind.value

        object_id = self.object_id

        effect = self.effect.value

        operator = self.operator

        decision = self.decision

        source = self.source.value

        source_ref = self.source_ref

        recorded_by = self.recorded_by

        created_at = self.created_at

        idempotency_key = self.idempotency_key

        releases: None | str | Unset
        if isinstance(self.releases, Unset):
            releases = UNSET
        else:
            releases = self.releases

        active: bool | None | Unset
        if isinstance(self.active, Unset):
            active = UNSET
        else:
            active = self.active

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "project_id": project_id,
                "object_kind": object_kind,
                "object_id": object_id,
                "effect": effect,
                "operator": operator,
                "decision": decision,
                "source": source,
                "source_ref": source_ref,
                "recorded_by": recorded_by,
                "created_at": created_at,
                "idempotency_key": idempotency_key,
            }
        )
        if releases is not UNSET:
            field_dict["releases"] = releases
        if active is not UNSET:
            field_dict["active"] = active

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        project_id = d.pop("project_id")

        object_kind = OperatorDecisionModelObjectKind(d.pop("object_kind"))

        object_id = d.pop("object_id")

        effect = OperatorDecisionModelEffect(d.pop("effect"))

        operator = d.pop("operator")

        decision = d.pop("decision")

        source = OperatorDecisionModelSource(d.pop("source"))

        source_ref = d.pop("source_ref")

        recorded_by = d.pop("recorded_by")

        created_at = d.pop("created_at")

        idempotency_key = d.pop("idempotency_key")

        def _parse_releases(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        releases = _parse_releases(d.pop("releases", UNSET))

        def _parse_active(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        active = _parse_active(d.pop("active", UNSET))

        operator_decision_model = cls(
            id=id,
            project_id=project_id,
            object_kind=object_kind,
            object_id=object_id,
            effect=effect,
            operator=operator,
            decision=decision,
            source=source,
            source_ref=source_ref,
            recorded_by=recorded_by,
            created_at=created_at,
            idempotency_key=idempotency_key,
            releases=releases,
            active=active,
        )

        operator_decision_model.additional_properties = d
        return operator_decision_model

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
