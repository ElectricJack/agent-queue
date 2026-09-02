from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.effect_clause_dto_kind import EffectClauseDTOKind
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.explanation_row_dto import ExplanationRowDTO


T = TypeVar("T", bound="EffectClauseDTO")


@_attrs_define
class EffectClauseDTO:
    """One typed effect clause from the command contract.

    ``detail`` is rendered by the backend from the clause and its resolved
    inputs.  The frontend lays this out; it never re-derives command meaning
    (design spec: "The frontend lays out this structure but does not
    reinterpret command semantics").

        Attributes:
            kind (EffectClauseDTOKind):
            subject (str):
            detail (str):
            arguments (list[ExplanationRowDTO] | Unset):
            conditional_on (None | str | Unset):
    """

    kind: EffectClauseDTOKind
    subject: str
    detail: str
    arguments: list[ExplanationRowDTO] | Unset = UNSET
    conditional_on: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        subject = self.subject

        detail = self.detail

        arguments: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.arguments, Unset):
            arguments = []
            for arguments_item_data in self.arguments:
                arguments_item = arguments_item_data.to_dict()
                arguments.append(arguments_item)

        conditional_on: None | str | Unset
        if isinstance(self.conditional_on, Unset):
            conditional_on = UNSET
        else:
            conditional_on = self.conditional_on

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "kind": kind,
                "subject": subject,
                "detail": detail,
            }
        )
        if arguments is not UNSET:
            field_dict["arguments"] = arguments
        if conditional_on is not UNSET:
            field_dict["conditional_on"] = conditional_on

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.explanation_row_dto import ExplanationRowDTO

        d = dict(src_dict)
        kind = EffectClauseDTOKind(d.pop("kind"))

        subject = d.pop("subject")

        detail = d.pop("detail")

        _arguments = d.pop("arguments", UNSET)
        arguments: list[ExplanationRowDTO] | Unset = UNSET
        if _arguments is not UNSET:
            arguments = []
            for arguments_item_data in _arguments:
                arguments_item = ExplanationRowDTO.from_dict(arguments_item_data)

                arguments.append(arguments_item)

        def _parse_conditional_on(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        conditional_on = _parse_conditional_on(d.pop("conditional_on", UNSET))

        effect_clause_dto = cls(
            kind=kind,
            subject=subject,
            detail=detail,
            arguments=arguments,
            conditional_on=conditional_on,
        )

        return effect_clause_dto
