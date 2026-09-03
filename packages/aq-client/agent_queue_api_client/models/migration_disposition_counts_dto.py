from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="MigrationDispositionCountsDTO")


@_attrs_define
class MigrationDispositionCountsDTO:
    """
    Attributes:
        ready (int):
        question_required (int):
        invalid (int):
        disabled (int):
    """

    ready: int
    question_required: int
    invalid: int
    disabled: int

    def to_dict(self) -> dict[str, Any]:
        ready = self.ready

        question_required = self.question_required

        invalid = self.invalid

        disabled = self.disabled

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "ready": ready,
                "question_required": question_required,
                "invalid": invalid,
                "disabled": disabled,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ready = d.pop("ready")

        question_required = d.pop("question_required")

        invalid = d.pop("invalid")

        disabled = d.pop("disabled")

        migration_disposition_counts_dto = cls(
            ready=ready,
            question_required=question_required,
            invalid=invalid,
            disabled=disabled,
        )

        return migration_disposition_counts_dto
