from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PoolProviderUnavailable")


@_attrs_define
class PoolProviderUnavailable:
    """The pool's provider is unavailable, so it is sized to zero (provider-failover D13).

    Distinct from ``placement_starved``: nothing is wrong with the pool, its
    projects or their workspaces -- the login it draws on is down.

        Attributes:
            provider (str):
            state (str):
            reason (str | Unset):  Default: ''.
            until (float | None | Unset):
    """

    provider: str
    state: str
    reason: str | Unset = ""
    until: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        provider = self.provider

        state = self.state

        reason = self.reason

        until: float | None | Unset
        if isinstance(self.until, Unset):
            until = UNSET
        else:
            until = self.until

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
                "state": state,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason
        if until is not UNSET:
            field_dict["until"] = until

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        provider = d.pop("provider")

        state = d.pop("state")

        reason = d.pop("reason", UNSET)

        def _parse_until(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        until = _parse_until(d.pop("until", UNSET))

        pool_provider_unavailable = cls(
            provider=provider,
            state=state,
            reason=reason,
            until=until,
        )

        pool_provider_unavailable.additional_properties = d
        return pool_provider_unavailable

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
