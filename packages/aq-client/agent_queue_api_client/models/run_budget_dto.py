from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="RunBudgetDTO")


@_attrs_define
class RunBudgetDTO:
    """
    Attributes:
        llm_calls (int | Unset):  Default: 0.
        total_tokens (int | Unset):  Default: 0.
        max_total_tokens (int | None | Unset):
        cost_usd (float | None | Unset):
    """

    llm_calls: int | Unset = 0
    total_tokens: int | Unset = 0
    max_total_tokens: int | None | Unset = UNSET
    cost_usd: float | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        llm_calls = self.llm_calls

        total_tokens = self.total_tokens

        max_total_tokens: int | None | Unset
        if isinstance(self.max_total_tokens, Unset):
            max_total_tokens = UNSET
        else:
            max_total_tokens = self.max_total_tokens

        cost_usd: float | None | Unset
        if isinstance(self.cost_usd, Unset):
            cost_usd = UNSET
        else:
            cost_usd = self.cost_usd

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if llm_calls is not UNSET:
            field_dict["llm_calls"] = llm_calls
        if total_tokens is not UNSET:
            field_dict["total_tokens"] = total_tokens
        if max_total_tokens is not UNSET:
            field_dict["max_total_tokens"] = max_total_tokens
        if cost_usd is not UNSET:
            field_dict["cost_usd"] = cost_usd

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        llm_calls = d.pop("llm_calls", UNSET)

        total_tokens = d.pop("total_tokens", UNSET)

        def _parse_max_total_tokens(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_total_tokens = _parse_max_total_tokens(d.pop("max_total_tokens", UNSET))

        def _parse_cost_usd(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        cost_usd = _parse_cost_usd(d.pop("cost_usd", UNSET))

        run_budget_dto = cls(
            llm_calls=llm_calls,
            total_tokens=total_tokens,
            max_total_tokens=max_total_tokens,
            cost_usd=cost_usd,
        )

        return run_budget_dto
