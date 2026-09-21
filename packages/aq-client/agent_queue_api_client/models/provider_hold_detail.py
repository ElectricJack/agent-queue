from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderHoldDetail")


@_attrs_define
class ProviderHoldDetail:
    """Why a queued task is held by its provider (provider-failover D18).

    ``kind`` is one of ``provider_pinned``, ``class_policy_hold``,
    ``no_equivalent_rung``, ``no_available_target``,
    ``awaiting_failover_capacity``, ``reroute_limit_reached``,
    ``all_providers_unavailable``, ``failover_inactive`` or
    ``priority_policy_hold``.  ``ahead`` is the queue position for
    ``awaiting_failover_capacity`` and ``None`` otherwise; ``detail`` says it
    in words.

        Attributes:
            provider (str):
            state (str):
            kind (str):
            vendor (str | Unset):  Default: ''.
            since (float | None | Unset):
            until (float | None | Unset):
            ahead (int | None | Unset):
            detail (str | Unset):  Default: ''.
            profile_id (None | str | Unset):
            reason (str | Unset):  Default: ''.
            remediation (str | Unset):  Default: ''.
    """

    provider: str
    state: str
    kind: str
    vendor: str | Unset = ""
    since: float | None | Unset = UNSET
    until: float | None | Unset = UNSET
    ahead: int | None | Unset = UNSET
    detail: str | Unset = ""
    profile_id: None | str | Unset = UNSET
    reason: str | Unset = ""
    remediation: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        provider = self.provider

        state = self.state

        kind = self.kind

        vendor = self.vendor

        since: float | None | Unset
        if isinstance(self.since, Unset):
            since = UNSET
        else:
            since = self.since

        until: float | None | Unset
        if isinstance(self.until, Unset):
            until = UNSET
        else:
            until = self.until

        ahead: int | None | Unset
        if isinstance(self.ahead, Unset):
            ahead = UNSET
        else:
            ahead = self.ahead

        detail = self.detail

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        reason = self.reason

        remediation = self.remediation

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
                "state": state,
                "kind": kind,
            }
        )
        if vendor is not UNSET:
            field_dict["vendor"] = vendor
        if since is not UNSET:
            field_dict["since"] = since
        if until is not UNSET:
            field_dict["until"] = until
        if ahead is not UNSET:
            field_dict["ahead"] = ahead
        if detail is not UNSET:
            field_dict["detail"] = detail
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if reason is not UNSET:
            field_dict["reason"] = reason
        if remediation is not UNSET:
            field_dict["remediation"] = remediation

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        provider = d.pop("provider")

        state = d.pop("state")

        kind = d.pop("kind")

        vendor = d.pop("vendor", UNSET)

        def _parse_since(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        since = _parse_since(d.pop("since", UNSET))

        def _parse_until(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        until = _parse_until(d.pop("until", UNSET))

        def _parse_ahead(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        ahead = _parse_ahead(d.pop("ahead", UNSET))

        detail = d.pop("detail", UNSET)

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        reason = d.pop("reason", UNSET)

        remediation = d.pop("remediation", UNSET)

        provider_hold_detail = cls(
            provider=provider,
            state=state,
            kind=kind,
            vendor=vendor,
            since=since,
            until=until,
            ahead=ahead,
            detail=detail,
            profile_id=profile_id,
            reason=reason,
            remediation=remediation,
        )

        provider_hold_detail.additional_properties = d
        return provider_hold_detail

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
