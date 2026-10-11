from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="AskSourceVersion")


@_attrs_define
class AskSourceVersion:
    """
    Attributes:
        decision_generation (int):
        source_fingerprint (str):
        feedback_version (int):
    """

    decision_generation: int
    source_fingerprint: str
    feedback_version: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        decision_generation = self.decision_generation

        source_fingerprint = self.source_fingerprint

        feedback_version = self.feedback_version

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "decision_generation": decision_generation,
                "source_fingerprint": source_fingerprint,
                "feedback_version": feedback_version,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        decision_generation = d.pop("decision_generation")

        source_fingerprint = d.pop("source_fingerprint")

        feedback_version = d.pop("feedback_version")

        ask_source_version = cls(
            decision_generation=decision_generation,
            source_fingerprint=source_fingerprint,
            feedback_version=feedback_version,
        )

        ask_source_version.additional_properties = d
        return ask_source_version

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
