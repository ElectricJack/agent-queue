from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ExplanationEffect")


@_attrs_define
class ExplanationEffect:
    """
    Attributes:
        operation (str):
        text (str):
        condition (None | str | Unset):
        subject (None | str | Unset):
    """

    operation: str
    text: str
    condition: None | str | Unset = UNSET
    subject: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        operation = self.operation

        text = self.text

        condition: None | str | Unset
        if isinstance(self.condition, Unset):
            condition = UNSET
        else:
            condition = self.condition

        subject: None | str | Unset
        if isinstance(self.subject, Unset):
            subject = UNSET
        else:
            subject = self.subject

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "operation": operation,
                "text": text,
            }
        )
        if condition is not UNSET:
            field_dict["condition"] = condition
        if subject is not UNSET:
            field_dict["subject"] = subject

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        operation = d.pop("operation")

        text = d.pop("text")

        def _parse_condition(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        condition = _parse_condition(d.pop("condition", UNSET))

        def _parse_subject(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        subject = _parse_subject(d.pop("subject", UNSET))

        explanation_effect = cls(
            operation=operation,
            text=text,
            condition=condition,
            subject=subject,
        )

        explanation_effect.additional_properties = d
        return explanation_effect

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
