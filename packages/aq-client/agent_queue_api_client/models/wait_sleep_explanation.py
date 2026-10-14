from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_sleep_explanation_inputs import WaitSleepExplanationInputs


T = TypeVar("T", bound="WaitSleepExplanation")


@_attrs_define
class WaitSleepExplanation:
    """
    Attributes:
        decision (None | str | Unset):
        reason (None | str | Unset):
        inputs (WaitSleepExplanationInputs | Unset):
    """

    decision: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    inputs: WaitSleepExplanationInputs | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        decision: None | str | Unset
        if isinstance(self.decision, Unset):
            decision = UNSET
        else:
            decision = self.decision

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        inputs: dict[str, Any] | Unset = UNSET
        if not isinstance(self.inputs, Unset):
            inputs = self.inputs.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if decision is not UNSET:
            field_dict["decision"] = decision
        if reason is not UNSET:
            field_dict["reason"] = reason
        if inputs is not UNSET:
            field_dict["inputs"] = inputs

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_sleep_explanation_inputs import WaitSleepExplanationInputs

        d = dict(src_dict)

        def _parse_decision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        decision = _parse_decision(d.pop("decision", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        _inputs = d.pop("inputs", UNSET)
        inputs: WaitSleepExplanationInputs | Unset
        if isinstance(_inputs, Unset):
            inputs = UNSET
        else:
            inputs = WaitSleepExplanationInputs.from_dict(_inputs)

        wait_sleep_explanation = cls(
            decision=decision,
            reason=reason,
            inputs=inputs,
        )

        wait_sleep_explanation.additional_properties = d
        return wait_sleep_explanation

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
