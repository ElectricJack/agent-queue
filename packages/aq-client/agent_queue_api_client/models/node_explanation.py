from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.explanation_effect import ExplanationEffect
    from ..models.explanation_input import ExplanationInput
    from ..models.explanation_loop import ExplanationLoop
    from ..models.explanation_outcome import ExplanationOutcome
    from ..models.explanation_result_binding import ExplanationResultBinding


T = TypeVar("T", bound="NodeExplanation")


@_attrs_define
class NodeExplanation:
    """
    Attributes:
        kind (str):
        title (str):
        command (None | str | Unset):
        contract_fingerprint (None | str | Unset):
        capability (None | str | Unset):
        effects (list[ExplanationEffect] | Unset):
        inputs (list[ExplanationInput] | Unset):
        result (ExplanationResultBinding | None | Unset):
        outcomes (list[ExplanationOutcome] | Unset):
        loop (ExplanationLoop | None | Unset):
        idempotency (None | str | Unset):
        retry (None | str | Unset):
        unrendered_fields (list[str] | Unset):
    """

    kind: str
    title: str
    command: None | str | Unset = UNSET
    contract_fingerprint: None | str | Unset = UNSET
    capability: None | str | Unset = UNSET
    effects: list[ExplanationEffect] | Unset = UNSET
    inputs: list[ExplanationInput] | Unset = UNSET
    result: ExplanationResultBinding | None | Unset = UNSET
    outcomes: list[ExplanationOutcome] | Unset = UNSET
    loop: ExplanationLoop | None | Unset = UNSET
    idempotency: None | str | Unset = UNSET
    retry: None | str | Unset = UNSET
    unrendered_fields: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.explanation_loop import ExplanationLoop
        from ..models.explanation_result_binding import ExplanationResultBinding

        kind = self.kind

        title = self.title

        command: None | str | Unset
        if isinstance(self.command, Unset):
            command = UNSET
        else:
            command = self.command

        contract_fingerprint: None | str | Unset
        if isinstance(self.contract_fingerprint, Unset):
            contract_fingerprint = UNSET
        else:
            contract_fingerprint = self.contract_fingerprint

        capability: None | str | Unset
        if isinstance(self.capability, Unset):
            capability = UNSET
        else:
            capability = self.capability

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
        elif isinstance(self.result, ExplanationResultBinding):
            result = self.result.to_dict()
        else:
            result = self.result

        outcomes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.outcomes, Unset):
            outcomes = []
            for outcomes_item_data in self.outcomes:
                outcomes_item = outcomes_item_data.to_dict()
                outcomes.append(outcomes_item)

        loop: dict[str, Any] | None | Unset
        if isinstance(self.loop, Unset):
            loop = UNSET
        elif isinstance(self.loop, ExplanationLoop):
            loop = self.loop.to_dict()
        else:
            loop = self.loop

        idempotency: None | str | Unset
        if isinstance(self.idempotency, Unset):
            idempotency = UNSET
        else:
            idempotency = self.idempotency

        retry: None | str | Unset
        if isinstance(self.retry, Unset):
            retry = UNSET
        else:
            retry = self.retry

        unrendered_fields: list[str] | Unset = UNSET
        if not isinstance(self.unrendered_fields, Unset):
            unrendered_fields = self.unrendered_fields

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "title": title,
            }
        )
        if command is not UNSET:
            field_dict["command"] = command
        if contract_fingerprint is not UNSET:
            field_dict["contract_fingerprint"] = contract_fingerprint
        if capability is not UNSET:
            field_dict["capability"] = capability
        if effects is not UNSET:
            field_dict["effects"] = effects
        if inputs is not UNSET:
            field_dict["inputs"] = inputs
        if result is not UNSET:
            field_dict["result"] = result
        if outcomes is not UNSET:
            field_dict["outcomes"] = outcomes
        if loop is not UNSET:
            field_dict["loop"] = loop
        if idempotency is not UNSET:
            field_dict["idempotency"] = idempotency
        if retry is not UNSET:
            field_dict["retry"] = retry
        if unrendered_fields is not UNSET:
            field_dict["unrendered_fields"] = unrendered_fields

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.explanation_effect import ExplanationEffect
        from ..models.explanation_input import ExplanationInput
        from ..models.explanation_loop import ExplanationLoop
        from ..models.explanation_outcome import ExplanationOutcome
        from ..models.explanation_result_binding import ExplanationResultBinding

        d = dict(src_dict)
        kind = d.pop("kind")

        title = d.pop("title")

        def _parse_command(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        command = _parse_command(d.pop("command", UNSET))

        def _parse_contract_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        contract_fingerprint = _parse_contract_fingerprint(d.pop("contract_fingerprint", UNSET))

        def _parse_capability(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        capability = _parse_capability(d.pop("capability", UNSET))

        _effects = d.pop("effects", UNSET)
        effects: list[ExplanationEffect] | Unset = UNSET
        if _effects is not UNSET:
            effects = []
            for effects_item_data in _effects:
                effects_item = ExplanationEffect.from_dict(effects_item_data)

                effects.append(effects_item)

        _inputs = d.pop("inputs", UNSET)
        inputs: list[ExplanationInput] | Unset = UNSET
        if _inputs is not UNSET:
            inputs = []
            for inputs_item_data in _inputs:
                inputs_item = ExplanationInput.from_dict(inputs_item_data)

                inputs.append(inputs_item)

        def _parse_result(data: object) -> ExplanationResultBinding | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = ExplanationResultBinding.from_dict(data)

                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ExplanationResultBinding | None | Unset, data)

        result = _parse_result(d.pop("result", UNSET))

        _outcomes = d.pop("outcomes", UNSET)
        outcomes: list[ExplanationOutcome] | Unset = UNSET
        if _outcomes is not UNSET:
            outcomes = []
            for outcomes_item_data in _outcomes:
                outcomes_item = ExplanationOutcome.from_dict(outcomes_item_data)

                outcomes.append(outcomes_item)

        def _parse_loop(data: object) -> ExplanationLoop | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                loop_type_0 = ExplanationLoop.from_dict(data)

                return loop_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ExplanationLoop | None | Unset, data)

        loop = _parse_loop(d.pop("loop", UNSET))

        def _parse_idempotency(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        idempotency = _parse_idempotency(d.pop("idempotency", UNSET))

        def _parse_retry(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        retry = _parse_retry(d.pop("retry", UNSET))

        unrendered_fields = cast(list[str], d.pop("unrendered_fields", UNSET))

        node_explanation = cls(
            kind=kind,
            title=title,
            command=command,
            contract_fingerprint=contract_fingerprint,
            capability=capability,
            effects=effects,
            inputs=inputs,
            result=result,
            outcomes=outcomes,
            loop=loop,
            idempotency=idempotency,
            retry=retry,
            unrendered_fields=unrendered_fields,
        )

        node_explanation.additional_properties = d
        return node_explanation

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
