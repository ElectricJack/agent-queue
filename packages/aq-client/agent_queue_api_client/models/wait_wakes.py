from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitWakes")


@_attrs_define
class WaitWakes:
    """
    Attributes:
        outcome (float | None | Unset):
        decision (float | None | Unset):
        lift (float | None | Unset):
        launch_failed (float | None | Unset):
        other (float | None | Unset):
    """

    outcome: float | None | Unset = UNSET
    decision: float | None | Unset = UNSET
    lift: float | None | Unset = UNSET
    launch_failed: float | None | Unset = UNSET
    other: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        outcome: float | None | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        else:
            outcome = self.outcome

        decision: float | None | Unset
        if isinstance(self.decision, Unset):
            decision = UNSET
        else:
            decision = self.decision

        lift: float | None | Unset
        if isinstance(self.lift, Unset):
            lift = UNSET
        else:
            lift = self.lift

        launch_failed: float | None | Unset
        if isinstance(self.launch_failed, Unset):
            launch_failed = UNSET
        else:
            launch_failed = self.launch_failed

        other: float | None | Unset
        if isinstance(self.other, Unset):
            other = UNSET
        else:
            other = self.other

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if decision is not UNSET:
            field_dict["decision"] = decision
        if lift is not UNSET:
            field_dict["lift"] = lift
        if launch_failed is not UNSET:
            field_dict["launch_failed"] = launch_failed
        if other is not UNSET:
            field_dict["other"] = other

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_outcome(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_decision(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        decision = _parse_decision(d.pop("decision", UNSET))

        def _parse_lift(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        lift = _parse_lift(d.pop("lift", UNSET))

        def _parse_launch_failed(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        launch_failed = _parse_launch_failed(d.pop("launch_failed", UNSET))

        def _parse_other(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        other = _parse_other(d.pop("other", UNSET))

        wait_wakes = cls(
            outcome=outcome,
            decision=decision,
            lift=lift,
            launch_failed=launch_failed,
            other=other,
        )

        wait_wakes.additional_properties = d
        return wait_wakes

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
