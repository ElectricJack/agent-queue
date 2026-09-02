from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.explanation_value_kind import ExplanationValueKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="ExplanationValue")


@_attrs_define
class ExplanationValue:
    """
    Attributes:
        kind (ExplanationValueKind):
        text (str):
        raw (None | str | Unset):
        redacted (bool | Unset):  Default: False.
    """

    kind: ExplanationValueKind
    text: str
    raw: None | str | Unset = UNSET
    redacted: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        text = self.text

        raw: None | str | Unset
        if isinstance(self.raw, Unset):
            raw = UNSET
        else:
            raw = self.raw

        redacted = self.redacted

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "kind": kind,
                "text": text,
            }
        )
        if raw is not UNSET:
            field_dict["raw"] = raw
        if redacted is not UNSET:
            field_dict["redacted"] = redacted

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = ExplanationValueKind(d.pop("kind"))

        text = d.pop("text")

        def _parse_raw(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        raw = _parse_raw(d.pop("raw", UNSET))

        redacted = d.pop("redacted", UNSET)

        explanation_value = cls(
            kind=kind,
            text=text,
            raw=raw,
            redacted=redacted,
        )

        explanation_value.additional_properties = d
        return explanation_value

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
