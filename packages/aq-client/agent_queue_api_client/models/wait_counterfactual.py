from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitCounterfactual")


@_attrs_define
class WaitCounterfactual:
    """Estimated avoided monitoring tokens, never measured token savings.

    Attributes:
        cohort (str):
        window (str):
        tokens (float | None | Unset):
    """

    cohort: str
    window: str
    tokens: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        cohort = self.cohort

        window = self.window

        tokens: float | None | Unset
        if isinstance(self.tokens, Unset):
            tokens = UNSET
        else:
            tokens = self.tokens

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "cohort": cohort,
                "window": window,
            }
        )
        if tokens is not UNSET:
            field_dict["tokens"] = tokens

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        cohort = d.pop("cohort")

        window = d.pop("window")

        def _parse_tokens(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        tokens = _parse_tokens(d.pop("tokens", UNSET))

        wait_counterfactual = cls(
            cohort=cohort,
            window=window,
            tokens=tokens,
        )

        wait_counterfactual.additional_properties = d
        return wait_counterfactual

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
