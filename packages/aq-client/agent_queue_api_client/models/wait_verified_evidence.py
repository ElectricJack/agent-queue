from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_verified_evidence_condition import WaitVerifiedEvidenceCondition
    from ..models.wait_verified_evidence_facts import WaitVerifiedEvidenceFacts


T = TypeVar("T", bound="WaitVerifiedEvidence")


@_attrs_define
class WaitVerifiedEvidence:
    """AQ observations only; claimed commentary is never a source fact.

    Attributes:
        kind (None | str | Unset):
        condition (WaitVerifiedEvidenceCondition | Unset):
        state (None | str | Unset):
        evidence (None | str | Unset):
        source_key (None | str | Unset):
        source_generation (int | None | Unset):
        source_revision (int | None | str | Unset):
        observed_at (float | None | Unset):
        freshness_seconds (float | None | Unset):
        facts (WaitVerifiedEvidenceFacts | Unset):
    """

    kind: None | str | Unset = UNSET
    condition: WaitVerifiedEvidenceCondition | Unset = UNSET
    state: None | str | Unset = UNSET
    evidence: None | str | Unset = UNSET
    source_key: None | str | Unset = UNSET
    source_generation: int | None | Unset = UNSET
    source_revision: int | None | str | Unset = UNSET
    observed_at: float | None | Unset = UNSET
    freshness_seconds: float | None | Unset = UNSET
    facts: WaitVerifiedEvidenceFacts | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind: None | str | Unset
        if isinstance(self.kind, Unset):
            kind = UNSET
        else:
            kind = self.kind

        condition: dict[str, Any] | Unset = UNSET
        if not isinstance(self.condition, Unset):
            condition = self.condition.to_dict()

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

        evidence: None | str | Unset
        if isinstance(self.evidence, Unset):
            evidence = UNSET
        else:
            evidence = self.evidence

        source_key: None | str | Unset
        if isinstance(self.source_key, Unset):
            source_key = UNSET
        else:
            source_key = self.source_key

        source_generation: int | None | Unset
        if isinstance(self.source_generation, Unset):
            source_generation = UNSET
        else:
            source_generation = self.source_generation

        source_revision: int | None | str | Unset
        if isinstance(self.source_revision, Unset):
            source_revision = UNSET
        else:
            source_revision = self.source_revision

        observed_at: float | None | Unset
        if isinstance(self.observed_at, Unset):
            observed_at = UNSET
        else:
            observed_at = self.observed_at

        freshness_seconds: float | None | Unset
        if isinstance(self.freshness_seconds, Unset):
            freshness_seconds = UNSET
        else:
            freshness_seconds = self.freshness_seconds

        facts: dict[str, Any] | Unset = UNSET
        if not isinstance(self.facts, Unset):
            facts = self.facts.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if kind is not UNSET:
            field_dict["kind"] = kind
        if condition is not UNSET:
            field_dict["condition"] = condition
        if state is not UNSET:
            field_dict["state"] = state
        if evidence is not UNSET:
            field_dict["evidence"] = evidence
        if source_key is not UNSET:
            field_dict["source_key"] = source_key
        if source_generation is not UNSET:
            field_dict["source_generation"] = source_generation
        if source_revision is not UNSET:
            field_dict["source_revision"] = source_revision
        if observed_at is not UNSET:
            field_dict["observed_at"] = observed_at
        if freshness_seconds is not UNSET:
            field_dict["freshness_seconds"] = freshness_seconds
        if facts is not UNSET:
            field_dict["facts"] = facts

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_verified_evidence_condition import WaitVerifiedEvidenceCondition
        from ..models.wait_verified_evidence_facts import WaitVerifiedEvidenceFacts

        d = dict(src_dict)

        def _parse_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        kind = _parse_kind(d.pop("kind", UNSET))

        _condition = d.pop("condition", UNSET)
        condition: WaitVerifiedEvidenceCondition | Unset
        if isinstance(_condition, Unset):
            condition = UNSET
        else:
            condition = WaitVerifiedEvidenceCondition.from_dict(_condition)

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        def _parse_evidence(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        evidence = _parse_evidence(d.pop("evidence", UNSET))

        def _parse_source_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_key = _parse_source_key(d.pop("source_key", UNSET))

        def _parse_source_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        source_generation = _parse_source_generation(d.pop("source_generation", UNSET))

        def _parse_source_revision(data: object) -> int | None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | str | Unset, data)

        source_revision = _parse_source_revision(d.pop("source_revision", UNSET))

        def _parse_observed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        observed_at = _parse_observed_at(d.pop("observed_at", UNSET))

        def _parse_freshness_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        freshness_seconds = _parse_freshness_seconds(d.pop("freshness_seconds", UNSET))

        _facts = d.pop("facts", UNSET)
        facts: WaitVerifiedEvidenceFacts | Unset
        if isinstance(_facts, Unset):
            facts = UNSET
        else:
            facts = WaitVerifiedEvidenceFacts.from_dict(_facts)

        wait_verified_evidence = cls(
            kind=kind,
            condition=condition,
            state=state,
            evidence=evidence,
            source_key=source_key,
            source_generation=source_generation,
            source_revision=source_revision,
            observed_at=observed_at,
            freshness_seconds=freshness_seconds,
            facts=facts,
        )

        wait_verified_evidence.additional_properties = d
        return wait_verified_evidence

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
