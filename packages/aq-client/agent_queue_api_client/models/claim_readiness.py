from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.claim_readiness_state import ClaimReadinessState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.claim_readiness_reason import ClaimReadinessReason


T = TypeVar("T", bound="ClaimReadiness")


@_attrs_define
class ClaimReadiness:
    """
    Attributes:
        state (ClaimReadinessState):
        claimable (bool):
        label (str):
        reason (str | Unset):  Default: ''.
        reasons (list[ClaimReadinessReason] | Unset):
    """

    state: ClaimReadinessState
    claimable: bool
    label: str
    reason: str | Unset = ""
    reasons: list[ClaimReadinessReason] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        state = self.state.value

        claimable = self.claimable

        label = self.label

        reason = self.reason

        reasons: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.reasons, Unset):
            reasons = []
            for reasons_item_data in self.reasons:
                reasons_item = reasons_item_data.to_dict()
                reasons.append(reasons_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "state": state,
                "claimable": claimable,
                "label": label,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason
        if reasons is not UNSET:
            field_dict["reasons"] = reasons

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.claim_readiness_reason import ClaimReadinessReason

        d = dict(src_dict)
        state = ClaimReadinessState(d.pop("state"))

        claimable = d.pop("claimable")

        label = d.pop("label")

        reason = d.pop("reason", UNSET)

        _reasons = d.pop("reasons", UNSET)
        reasons: list[ClaimReadinessReason] | Unset = UNSET
        if _reasons is not UNSET:
            reasons = []
            for reasons_item_data in _reasons:
                reasons_item = ClaimReadinessReason.from_dict(reasons_item_data)

                reasons.append(reasons_item)

        claim_readiness = cls(
            state=state,
            claimable=claimable,
            label=label,
            reason=reason,
            reasons=reasons,
        )

        claim_readiness.additional_properties = d
        return claim_readiness

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
