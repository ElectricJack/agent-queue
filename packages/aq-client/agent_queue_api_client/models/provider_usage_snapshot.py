from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderUsageSnapshot")


@_attrs_define
class ProviderUsageSnapshot:
    """One observation of one limit window.

    ``resets_at`` is nullable because a percentage without a clock still beats
    no reading at all --- the providers do not always print a reset clause.

        Attributes:
            id (int):
            provider (str):
            window (str):
            used_percent (float):
            observed_at (float):
            last_seen_at (float):
            source (str):
            account_label (str | Unset):  Default: ''.
            scope (str | Unset):  Default: ''.
            resets_at (float | None | Unset):
            stale (bool | Unset):  Default: False.
            age_seconds (float | Unset):  Default: 0.0.
    """

    id: int
    provider: str
    window: str
    used_percent: float
    observed_at: float
    last_seen_at: float
    source: str
    account_label: str | Unset = ""
    scope: str | Unset = ""
    resets_at: float | None | Unset = UNSET
    stale: bool | Unset = False
    age_seconds: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        provider = self.provider

        window = self.window

        used_percent = self.used_percent

        observed_at = self.observed_at

        last_seen_at = self.last_seen_at

        source = self.source

        account_label = self.account_label

        scope = self.scope

        resets_at: float | None | Unset
        if isinstance(self.resets_at, Unset):
            resets_at = UNSET
        else:
            resets_at = self.resets_at

        stale = self.stale

        age_seconds = self.age_seconds

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "provider": provider,
                "window": window,
                "used_percent": used_percent,
                "observed_at": observed_at,
                "last_seen_at": last_seen_at,
                "source": source,
            }
        )
        if account_label is not UNSET:
            field_dict["account_label"] = account_label
        if scope is not UNSET:
            field_dict["scope"] = scope
        if resets_at is not UNSET:
            field_dict["resets_at"] = resets_at
        if stale is not UNSET:
            field_dict["stale"] = stale
        if age_seconds is not UNSET:
            field_dict["age_seconds"] = age_seconds

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        provider = d.pop("provider")

        window = d.pop("window")

        used_percent = d.pop("used_percent")

        observed_at = d.pop("observed_at")

        last_seen_at = d.pop("last_seen_at")

        source = d.pop("source")

        account_label = d.pop("account_label", UNSET)

        scope = d.pop("scope", UNSET)

        def _parse_resets_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        resets_at = _parse_resets_at(d.pop("resets_at", UNSET))

        stale = d.pop("stale", UNSET)

        age_seconds = d.pop("age_seconds", UNSET)

        provider_usage_snapshot = cls(
            id=id,
            provider=provider,
            window=window,
            used_percent=used_percent,
            observed_at=observed_at,
            last_seen_at=last_seen_at,
            source=source,
            account_label=account_label,
            scope=scope,
            resets_at=resets_at,
            stale=stale,
            age_seconds=age_seconds,
        )

        provider_usage_snapshot.additional_properties = d
        return provider_usage_snapshot

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
