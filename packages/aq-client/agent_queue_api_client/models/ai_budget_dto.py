from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="AiBudgetDTO")


@_attrs_define
class AiBudgetDTO:
    """
    Attributes:
        max_calls (int | None | Unset):
        max_output_tokens (int | None | Unset):
        max_total_tokens (int | None | Unset):
        timeout_seconds (int | None | Unset):
    """

    max_calls: int | None | Unset = UNSET
    max_output_tokens: int | None | Unset = UNSET
    max_total_tokens: int | None | Unset = UNSET
    timeout_seconds: int | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        max_calls: int | None | Unset
        if isinstance(self.max_calls, Unset):
            max_calls = UNSET
        else:
            max_calls = self.max_calls

        max_output_tokens: int | None | Unset
        if isinstance(self.max_output_tokens, Unset):
            max_output_tokens = UNSET
        else:
            max_output_tokens = self.max_output_tokens

        max_total_tokens: int | None | Unset
        if isinstance(self.max_total_tokens, Unset):
            max_total_tokens = UNSET
        else:
            max_total_tokens = self.max_total_tokens

        timeout_seconds: int | None | Unset
        if isinstance(self.timeout_seconds, Unset):
            timeout_seconds = UNSET
        else:
            timeout_seconds = self.timeout_seconds

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if max_calls is not UNSET:
            field_dict["max_calls"] = max_calls
        if max_output_tokens is not UNSET:
            field_dict["max_output_tokens"] = max_output_tokens
        if max_total_tokens is not UNSET:
            field_dict["max_total_tokens"] = max_total_tokens
        if timeout_seconds is not UNSET:
            field_dict["timeout_seconds"] = timeout_seconds

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_max_calls(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_calls = _parse_max_calls(d.pop("max_calls", UNSET))

        def _parse_max_output_tokens(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_output_tokens = _parse_max_output_tokens(d.pop("max_output_tokens", UNSET))

        def _parse_max_total_tokens(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_total_tokens = _parse_max_total_tokens(d.pop("max_total_tokens", UNSET))

        def _parse_timeout_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        timeout_seconds = _parse_timeout_seconds(d.pop("timeout_seconds", UNSET))

        ai_budget_dto = cls(
            max_calls=max_calls,
            max_output_tokens=max_output_tokens,
            max_total_tokens=max_total_tokens,
            timeout_seconds=timeout_seconds,
        )

        return ai_budget_dto
