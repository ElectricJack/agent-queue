from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="RetryPolicyDTO")


@_attrs_define
class RetryPolicyDTO:
    """
    Attributes:
        max_attempts (int | Unset):  Default: 1.
        backoff_seconds (float | None | Unset):
        retry_on (list[str] | Unset):
    """

    max_attempts: int | Unset = 1
    backoff_seconds: float | None | Unset = UNSET
    retry_on: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        max_attempts = self.max_attempts

        backoff_seconds: float | None | Unset
        if isinstance(self.backoff_seconds, Unset):
            backoff_seconds = UNSET
        else:
            backoff_seconds = self.backoff_seconds

        retry_on: list[str] | Unset = UNSET
        if not isinstance(self.retry_on, Unset):
            retry_on = self.retry_on

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if max_attempts is not UNSET:
            field_dict["max_attempts"] = max_attempts
        if backoff_seconds is not UNSET:
            field_dict["backoff_seconds"] = backoff_seconds
        if retry_on is not UNSET:
            field_dict["retry_on"] = retry_on

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        max_attempts = d.pop("max_attempts", UNSET)

        def _parse_backoff_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        backoff_seconds = _parse_backoff_seconds(d.pop("backoff_seconds", UNSET))

        retry_on = cast(list[str], d.pop("retry_on", UNSET))

        retry_policy_dto = cls(
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
            retry_on=retry_on,
        )

        return retry_policy_dto
