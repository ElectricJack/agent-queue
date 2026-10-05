from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.operator_decision_record_effect import OperatorDecisionRecordEffect
from ..models.operator_decision_record_object_kind import OperatorDecisionRecordObjectKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="OperatorDecisionRecord")


@_attrs_define
class OperatorDecisionRecord:
    """One ``operator_decisions`` row; ``active`` is present on history reads.

    Attributes:
        id (str):
        project_id (str):
        object_kind (OperatorDecisionRecordObjectKind):
        object_id (str):
        effect (OperatorDecisionRecordEffect):
        operator (str):
        decision (str):
        source (str):
        source_ref (str):
        recorded_by (str):
        created_at (float):
        idempotency_key (str):
        releases (None | str | Unset):
        active (bool | None | Unset):
    """

    id: str
    project_id: str
    object_kind: OperatorDecisionRecordObjectKind
    object_id: str
    effect: OperatorDecisionRecordEffect
    operator: str
    decision: str
    source: str
    source_ref: str
    recorded_by: str
    created_at: float
    idempotency_key: str
    releases: None | str | Unset = UNSET
    active: bool | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        project_id = self.project_id

        object_kind = self.object_kind.value

        object_id = self.object_id

        effect = self.effect.value

        operator = self.operator

        decision = self.decision

        source = self.source

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

        object_kind = OperatorDecisionRecordObjectKind(d.pop("object_kind"))

        object_id = d.pop("object_id")

        effect = OperatorDecisionRecordEffect(d.pop("effect"))

        operator = d.pop("operator")

        decision = d.pop("decision")

        source = d.pop("source")

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

        operator_decision_record = cls(
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

        return operator_decision_record
