from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="Omission")


@_attrs_define
class Omission:
    """
    Attributes:
        min_probability (float):
        min_confidence (float):
        choice (Literal['unaffected'] | Unset):  Default: 'unaffected'.
    """

    min_probability: float
    min_confidence: float
    choice: Literal["unaffected"] | Unset = "unaffected"

    def to_dict(self) -> dict[str, Any]:
        min_probability = self.min_probability

        min_confidence = self.min_confidence

        choice = self.choice

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "min_probability": min_probability,
                "min_confidence": min_confidence,
            }
        )
        if choice is not UNSET:
            field_dict["choice"] = choice

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        min_probability = d.pop("min_probability")

        min_confidence = d.pop("min_confidence")

        choice = cast(Literal["unaffected"] | Unset, d.pop("choice", UNSET))
        if choice != "unaffected" and not isinstance(choice, Unset):
            raise ValueError(f"choice must match const 'unaffected', got '{choice}'")

        omission = cls(
            min_probability=min_probability,
            min_confidence=min_confidence,
            choice=choice,
        )

        return omission
