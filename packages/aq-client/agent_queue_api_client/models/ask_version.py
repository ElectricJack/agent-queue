from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="AskVersion")


@_attrs_define
class AskVersion:
    """
    Attributes:
        decision_generation (int):
        source_fingerprint (str):
        feedback_version (int):
    """

    decision_generation: int
    source_fingerprint: str
    feedback_version: int

    def to_dict(self) -> dict[str, Any]:
        decision_generation = self.decision_generation

        source_fingerprint = self.source_fingerprint

        feedback_version = self.feedback_version

        field_dict: dict[str, Any] = {}

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

        ask_version = cls(
            decision_generation=decision_generation,
            source_fingerprint=source_fingerprint,
            feedback_version=feedback_version,
        )

        return ask_version
