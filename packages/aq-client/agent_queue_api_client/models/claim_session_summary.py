from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.claim_session_summary_failed_preparation_type_0 import ClaimSessionSummaryFailedPreparationType0


T = TypeVar("T", bound="ClaimSessionSummary")


@_attrs_define
class ClaimSessionSummary:
    """The calling session's claim bookkeeping, echoed back on every ``task_claim`` reply.

    Attributes:
        id (None | str | Unset):
        claims (int | None | Unset):
        cap (int | None | Unset):
        desired_state (None | str | Unset):
        claim_phase (None | str | Unset):
        failed_preparation (ClaimSessionSummaryFailedPreparationType0 | None | Unset):
    """

    id: None | str | Unset = UNSET
    claims: int | None | Unset = UNSET
    cap: int | None | Unset = UNSET
    desired_state: None | str | Unset = UNSET
    claim_phase: None | str | Unset = UNSET
    failed_preparation: ClaimSessionSummaryFailedPreparationType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.claim_session_summary_failed_preparation_type_0 import ClaimSessionSummaryFailedPreparationType0

        id: None | str | Unset
        if isinstance(self.id, Unset):
            id = UNSET
        else:
            id = self.id

        claims: int | None | Unset
        if isinstance(self.claims, Unset):
            claims = UNSET
        else:
            claims = self.claims

        cap: int | None | Unset
        if isinstance(self.cap, Unset):
            cap = UNSET
        else:
            cap = self.cap

        desired_state: None | str | Unset
        if isinstance(self.desired_state, Unset):
            desired_state = UNSET
        else:
            desired_state = self.desired_state

        claim_phase: None | str | Unset
        if isinstance(self.claim_phase, Unset):
            claim_phase = UNSET
        else:
            claim_phase = self.claim_phase

        failed_preparation: dict[str, Any] | None | Unset
        if isinstance(self.failed_preparation, Unset):
            failed_preparation = UNSET
        elif isinstance(self.failed_preparation, ClaimSessionSummaryFailedPreparationType0):
            failed_preparation = self.failed_preparation.to_dict()
        else:
            failed_preparation = self.failed_preparation

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if id is not UNSET:
            field_dict["id"] = id
        if claims is not UNSET:
            field_dict["claims"] = claims
        if cap is not UNSET:
            field_dict["cap"] = cap
        if desired_state is not UNSET:
            field_dict["desired_state"] = desired_state
        if claim_phase is not UNSET:
            field_dict["claim_phase"] = claim_phase
        if failed_preparation is not UNSET:
            field_dict["failed_preparation"] = failed_preparation

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.claim_session_summary_failed_preparation_type_0 import ClaimSessionSummaryFailedPreparationType0

        d = dict(src_dict)

        def _parse_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        id = _parse_id(d.pop("id", UNSET))

        def _parse_claims(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claims = _parse_claims(d.pop("claims", UNSET))

        def _parse_cap(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        cap = _parse_cap(d.pop("cap", UNSET))

        def _parse_desired_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        desired_state = _parse_desired_state(d.pop("desired_state", UNSET))

        def _parse_claim_phase(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        claim_phase = _parse_claim_phase(d.pop("claim_phase", UNSET))

        def _parse_failed_preparation(data: object) -> ClaimSessionSummaryFailedPreparationType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failed_preparation_type_0 = ClaimSessionSummaryFailedPreparationType0.from_dict(data)

                return failed_preparation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ClaimSessionSummaryFailedPreparationType0 | None | Unset, data)

        failed_preparation = _parse_failed_preparation(d.pop("failed_preparation", UNSET))

        claim_session_summary = cls(
            id=id,
            claims=claims,
            cap=cap,
            desired_state=desired_state,
            claim_phase=claim_phase,
            failed_preparation=failed_preparation,
        )

        claim_session_summary.additional_properties = d
        return claim_session_summary

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
