from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.step_explanation_dto_renderer import StepExplanationDTORenderer
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.effect_clause_dto import EffectClauseDTO
    from ..models.explanation_row_dto import ExplanationRowDTO
    from ..models.outcome_explanation_dto import OutcomeExplanationDTO


T = TypeVar("T", bound="StepExplanationDTO")


@_attrs_define
class StepExplanationDTO:
    """The contract-derived intent card.  Node card and inspector consume
    this same object (design spec UI invariant).

    ``renderer="canonical"`` is the spec's lossless fallback: presentation
    metadata was absent, so every executable field is shown as a field/value
    pair.  It is a display fact, never a reason to hide a field, and never
    blocks activation.

        Attributes:
            title (str):
            effect_summary (str):
            effects (list[EffectClauseDTO] | Unset):
            inputs (list[ExplanationRowDTO] | Unset):
            result (ExplanationRowDTO | None | Unset):
            outcomes (list[OutcomeExplanationDTO] | Unset):
            contract_fingerprint (None | str | Unset):
            renderer (StepExplanationDTORenderer | Unset):  Default: StepExplanationDTORenderer.CONTRACT.
    """

    title: str
    effect_summary: str
    effects: list[EffectClauseDTO] | Unset = UNSET
    inputs: list[ExplanationRowDTO] | Unset = UNSET
    result: ExplanationRowDTO | None | Unset = UNSET
    outcomes: list[OutcomeExplanationDTO] | Unset = UNSET
    contract_fingerprint: None | str | Unset = UNSET
    renderer: StepExplanationDTORenderer | Unset = StepExplanationDTORenderer.CONTRACT

    def to_dict(self) -> dict[str, Any]:
        from ..models.explanation_row_dto import ExplanationRowDTO

        title = self.title

        effect_summary = self.effect_summary

        effects: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.effects, Unset):
            effects = []
            for effects_item_data in self.effects:
                effects_item = effects_item_data.to_dict()
                effects.append(effects_item)

        inputs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.inputs, Unset):
            inputs = []
            for inputs_item_data in self.inputs:
                inputs_item = inputs_item_data.to_dict()
                inputs.append(inputs_item)

        result: dict[str, Any] | None | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, ExplanationRowDTO):
            result = self.result.to_dict()
        else:
            result = self.result

        outcomes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.outcomes, Unset):
            outcomes = []
            for outcomes_item_data in self.outcomes:
                outcomes_item = outcomes_item_data.to_dict()
                outcomes.append(outcomes_item)

        contract_fingerprint: None | str | Unset
        if isinstance(self.contract_fingerprint, Unset):
            contract_fingerprint = UNSET
        else:
            contract_fingerprint = self.contract_fingerprint

        renderer: str | Unset = UNSET
        if not isinstance(self.renderer, Unset):
            renderer = self.renderer.value

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "title": title,
                "effect_summary": effect_summary,
            }
        )
        if effects is not UNSET:
            field_dict["effects"] = effects
        if inputs is not UNSET:
            field_dict["inputs"] = inputs
        if result is not UNSET:
            field_dict["result"] = result
        if outcomes is not UNSET:
            field_dict["outcomes"] = outcomes
        if contract_fingerprint is not UNSET:
            field_dict["contract_fingerprint"] = contract_fingerprint
        if renderer is not UNSET:
            field_dict["renderer"] = renderer

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.effect_clause_dto import EffectClauseDTO
        from ..models.explanation_row_dto import ExplanationRowDTO
        from ..models.outcome_explanation_dto import OutcomeExplanationDTO

        d = dict(src_dict)
        title = d.pop("title")

        effect_summary = d.pop("effect_summary")

        _effects = d.pop("effects", UNSET)
        effects: list[EffectClauseDTO] | Unset = UNSET
        if _effects is not UNSET:
            effects = []
            for effects_item_data in _effects:
                effects_item = EffectClauseDTO.from_dict(effects_item_data)

                effects.append(effects_item)

        _inputs = d.pop("inputs", UNSET)
        inputs: list[ExplanationRowDTO] | Unset = UNSET
        if _inputs is not UNSET:
            inputs = []
            for inputs_item_data in _inputs:
                inputs_item = ExplanationRowDTO.from_dict(inputs_item_data)

                inputs.append(inputs_item)

        def _parse_result(data: object) -> ExplanationRowDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = ExplanationRowDTO.from_dict(data)

                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ExplanationRowDTO | None | Unset, data)

        result = _parse_result(d.pop("result", UNSET))

        _outcomes = d.pop("outcomes", UNSET)
        outcomes: list[OutcomeExplanationDTO] | Unset = UNSET
        if _outcomes is not UNSET:
            outcomes = []
            for outcomes_item_data in _outcomes:
                outcomes_item = OutcomeExplanationDTO.from_dict(outcomes_item_data)

                outcomes.append(outcomes_item)

        def _parse_contract_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        contract_fingerprint = _parse_contract_fingerprint(d.pop("contract_fingerprint", UNSET))

        _renderer = d.pop("renderer", UNSET)
        renderer: StepExplanationDTORenderer | Unset
        if isinstance(_renderer, Unset):
            renderer = UNSET
        else:
            renderer = StepExplanationDTORenderer(_renderer)

        step_explanation_dto = cls(
            title=title,
            effect_summary=effect_summary,
            effects=effects,
            inputs=inputs,
            result=result,
            outcomes=outcomes,
            contract_fingerprint=contract_fingerprint,
            renderer=renderer,
        )

        return step_explanation_dto
