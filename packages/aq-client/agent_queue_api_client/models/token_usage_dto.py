from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="TokenUsageDTO")


@_attrs_define
class TokenUsageDTO:
    """
    Attributes:
        input_tokens (int | Unset):  Default: 0.
        output_tokens (int | Unset):  Default: 0.
        total_tokens (int | Unset):  Default: 0.
        estimated (bool | Unset):  Default: False.
    """

    input_tokens: int | Unset = 0
    output_tokens: int | Unset = 0
    total_tokens: int | Unset = 0
    estimated: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        input_tokens = self.input_tokens

        output_tokens = self.output_tokens

        total_tokens = self.total_tokens

        estimated = self.estimated

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if input_tokens is not UNSET:
            field_dict["input_tokens"] = input_tokens
        if output_tokens is not UNSET:
            field_dict["output_tokens"] = output_tokens
        if total_tokens is not UNSET:
            field_dict["total_tokens"] = total_tokens
        if estimated is not UNSET:
            field_dict["estimated"] = estimated

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        input_tokens = d.pop("input_tokens", UNSET)

        output_tokens = d.pop("output_tokens", UNSET)

        total_tokens = d.pop("total_tokens", UNSET)

        estimated = d.pop("estimated", UNSET)

        token_usage_dto = cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            estimated=estimated,
        )

        return token_usage_dto
