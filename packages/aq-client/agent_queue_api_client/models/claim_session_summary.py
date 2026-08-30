from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

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
    """

    id: None | str | Unset = UNSET
    claims: int | None | Unset = UNSET
    cap: int | None | Unset = UNSET
    desired_state: None | str | Unset = UNSET
    claim_phase: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
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

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
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

        claim_session_summary = cls(
            id=id,
            claims=claims,
            cap=cap,
            desired_state=desired_state,
            claim_phase=claim_phase,
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
