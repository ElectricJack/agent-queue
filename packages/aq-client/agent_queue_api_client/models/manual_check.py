from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="ManualCheck")


@_attrs_define
class ManualCheck:
    """
    Attributes:
        action (str):
        surface (str):
        expected_result (str):
        reason (str):
        refs (list[str]):
        prior_verification (str):
        confidence (str):
    """

    action: str
    surface: str
    expected_result: str
    reason: str
    refs: list[str]
    prior_verification: str
    confidence: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        action = self.action

        surface = self.surface

        expected_result = self.expected_result

        reason = self.reason

        refs = self.refs

        prior_verification = self.prior_verification

        confidence = self.confidence

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "action": action,
                "surface": surface,
                "expected_result": expected_result,
                "reason": reason,
                "refs": refs,
                "prior_verification": prior_verification,
                "confidence": confidence,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        action = d.pop("action")

        surface = d.pop("surface")

        expected_result = d.pop("expected_result")

        reason = d.pop("reason")

        refs = cast(list[str], d.pop("refs"))

        prior_verification = d.pop("prior_verification")

        confidence = d.pop("confidence")

        manual_check = cls(
            action=action,
            surface=surface,
            expected_result=expected_result,
            reason=reason,
            refs=refs,
            prior_verification=prior_verification,
            confidence=confidence,
        )

        manual_check.additional_properties = d
        return manual_check

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
