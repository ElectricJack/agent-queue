from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeliveryLatencyWait")


@_attrs_define
class DeliveryLatencyWait:
    """A wait record linked to a generation (its task, batch or repair).

    ``segment`` names the breakdown segment an ``integration`` wait counts
    toward; ``ci_url`` is the exact CI run, set only for an http(s) result.

        Attributes:
            wait_id (str):
            origin (str):
            condition_kind (str):
            outcome (str):
            created_at (float):
            segment (None | str | Unset):
            resolved_at (float | None | Unset):
            evidence_ref (None | str | Unset):
            result_ref (None | str | Unset):
            decision_reason (None | str | Unset):
            ci_url (None | str | Unset):
    """

    wait_id: str
    origin: str
    condition_kind: str
    outcome: str
    created_at: float
    segment: None | str | Unset = UNSET
    resolved_at: float | None | Unset = UNSET
    evidence_ref: None | str | Unset = UNSET
    result_ref: None | str | Unset = UNSET
    decision_reason: None | str | Unset = UNSET
    ci_url: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        wait_id = self.wait_id

        origin = self.origin

        condition_kind = self.condition_kind

        outcome = self.outcome

        created_at = self.created_at

        segment: None | str | Unset
        if isinstance(self.segment, Unset):
            segment = UNSET
        else:
            segment = self.segment

        resolved_at: float | None | Unset
        if isinstance(self.resolved_at, Unset):
            resolved_at = UNSET
        else:
            resolved_at = self.resolved_at

        evidence_ref: None | str | Unset
        if isinstance(self.evidence_ref, Unset):
            evidence_ref = UNSET
        else:
            evidence_ref = self.evidence_ref

        result_ref: None | str | Unset
        if isinstance(self.result_ref, Unset):
            result_ref = UNSET
        else:
            result_ref = self.result_ref

        decision_reason: None | str | Unset
        if isinstance(self.decision_reason, Unset):
            decision_reason = UNSET
        else:
            decision_reason = self.decision_reason

        ci_url: None | str | Unset
        if isinstance(self.ci_url, Unset):
            ci_url = UNSET
        else:
            ci_url = self.ci_url

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "wait_id": wait_id,
                "origin": origin,
                "condition_kind": condition_kind,
                "outcome": outcome,
                "created_at": created_at,
            }
        )
        if segment is not UNSET:
            field_dict["segment"] = segment
        if resolved_at is not UNSET:
            field_dict["resolved_at"] = resolved_at
        if evidence_ref is not UNSET:
            field_dict["evidence_ref"] = evidence_ref
        if result_ref is not UNSET:
            field_dict["result_ref"] = result_ref
        if decision_reason is not UNSET:
            field_dict["decision_reason"] = decision_reason
        if ci_url is not UNSET:
            field_dict["ci_url"] = ci_url

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        wait_id = d.pop("wait_id")

        origin = d.pop("origin")

        condition_kind = d.pop("condition_kind")

        outcome = d.pop("outcome")

        created_at = d.pop("created_at")

        def _parse_segment(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        segment = _parse_segment(d.pop("segment", UNSET))

        def _parse_resolved_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        resolved_at = _parse_resolved_at(d.pop("resolved_at", UNSET))

        def _parse_evidence_ref(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        evidence_ref = _parse_evidence_ref(d.pop("evidence_ref", UNSET))

        def _parse_result_ref(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        result_ref = _parse_result_ref(d.pop("result_ref", UNSET))

        def _parse_decision_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        decision_reason = _parse_decision_reason(d.pop("decision_reason", UNSET))

        def _parse_ci_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ci_url = _parse_ci_url(d.pop("ci_url", UNSET))

        delivery_latency_wait = cls(
            wait_id=wait_id,
            origin=origin,
            condition_kind=condition_kind,
            outcome=outcome,
            created_at=created_at,
            segment=segment,
            resolved_at=resolved_at,
            evidence_ref=evidence_ref,
            result_ref=result_ref,
            decision_reason=decision_reason,
            ci_url=ci_url,
        )

        delivery_latency_wait.additional_properties = d
        return delivery_latency_wait

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
