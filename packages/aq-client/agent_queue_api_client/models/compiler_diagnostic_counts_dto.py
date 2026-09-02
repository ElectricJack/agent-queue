from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="CompilerDiagnosticCountsDTO")


@_attrs_define
class CompilerDiagnosticCountsDTO:
    """
    Attributes:
        error (int | Unset):  Default: 0.
        warning (int | Unset):  Default: 0.
        question (int | Unset):  Default: 0.
        info (int | Unset):  Default: 0.
    """

    error: int | Unset = 0
    warning: int | Unset = 0
    question: int | Unset = 0
    info: int | Unset = 0

    def to_dict(self) -> dict[str, Any]:
        error = self.error

        warning = self.warning

        question = self.question

        info = self.info

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if error is not UNSET:
            field_dict["error"] = error
        if warning is not UNSET:
            field_dict["warning"] = warning
        if question is not UNSET:
            field_dict["question"] = question
        if info is not UNSET:
            field_dict["info"] = info

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        error = d.pop("error", UNSET)

        warning = d.pop("warning", UNSET)

        question = d.pop("question", UNSET)

        info = d.pop("info", UNSET)

        compiler_diagnostic_counts_dto = cls(
            error=error,
            warning=warning,
            question=question,
            info=info,
        )

        return compiler_diagnostic_counts_dto
