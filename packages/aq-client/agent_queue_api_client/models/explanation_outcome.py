from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.explanation_outcome_classification import ExplanationOutcomeClassification
from ..types import UNSET, Unset

T = TypeVar("T", bound="ExplanationOutcome")


@_attrs_define
class ExplanationOutcome:
    """
    Attributes:
        outcome (str):
        label (str):
        classification (ExplanationOutcomeClassification):
        target_node_id (None | str | Unset):
        target_label (None | str | Unset):
    """

    outcome: str
    label: str
    classification: ExplanationOutcomeClassification
    target_node_id: None | str | Unset = UNSET
    target_label: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        outcome = self.outcome

        label = self.label

        classification = self.classification.value

        target_node_id: None | str | Unset
        if isinstance(self.target_node_id, Unset):
            target_node_id = UNSET
        else:
            target_node_id = self.target_node_id

        target_label: None | str | Unset
        if isinstance(self.target_label, Unset):
            target_label = UNSET
        else:
            target_label = self.target_label

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "outcome": outcome,
                "label": label,
                "classification": classification,
            }
        )
        if target_node_id is not UNSET:
            field_dict["target_node_id"] = target_node_id
        if target_label is not UNSET:
            field_dict["target_label"] = target_label

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        outcome = d.pop("outcome")

        label = d.pop("label")

        classification = ExplanationOutcomeClassification(d.pop("classification"))

        def _parse_target_node_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        target_node_id = _parse_target_node_id(d.pop("target_node_id", UNSET))

        def _parse_target_label(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        target_label = _parse_target_label(d.pop("target_label", UNSET))

        explanation_outcome = cls(
            outcome=outcome,
            label=label,
            classification=classification,
            target_node_id=target_node_id,
            target_label=target_label,
        )

        explanation_outcome.additional_properties = d
        return explanation_outcome

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
