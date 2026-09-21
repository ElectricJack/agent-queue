from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderStateRequest")


@_attrs_define
class ProviderStateRequest:
    """``POST /api/providers/{provider}/state`` (D6).

    ``for`` is a duration (``90s``, ``30m``, ``4h``, ``2d``, or seconds);
    ``until`` an epoch or ISO-8601 time.  Give at most one of ``for``,
    ``until`` and ``no_expiry``.

        Attributes:
            state (str):
            reason (None | str | Unset):
            for_ (None | str | Unset):
            until (None | str | Unset):
            no_expiry (bool | None | Unset):
    """

    state: str
    reason: None | str | Unset = UNSET
    for_: None | str | Unset = UNSET
    until: None | str | Unset = UNSET
    no_expiry: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        state = self.state

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        for_: None | str | Unset
        if isinstance(self.for_, Unset):
            for_ = UNSET
        else:
            for_ = self.for_

        until: None | str | Unset
        if isinstance(self.until, Unset):
            until = UNSET
        else:
            until = self.until

        no_expiry: bool | None | Unset
        if isinstance(self.no_expiry, Unset):
            no_expiry = UNSET
        else:
            no_expiry = self.no_expiry

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "state": state,
            }
        )
        if reason is not UNSET:
            field_dict["reason"] = reason
        if for_ is not UNSET:
            field_dict["for"] = for_
        if until is not UNSET:
            field_dict["until"] = until
        if no_expiry is not UNSET:
            field_dict["no_expiry"] = no_expiry

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        state = d.pop("state")

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_for_(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        for_ = _parse_for_(d.pop("for", UNSET))

        def _parse_until(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        until = _parse_until(d.pop("until", UNSET))

        def _parse_no_expiry(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        no_expiry = _parse_no_expiry(d.pop("no_expiry", UNSET))

        provider_state_request = cls(
            state=state,
            reason=reason,
            for_=for_,
            until=until,
            no_expiry=no_expiry,
        )

        provider_state_request.additional_properties = d
        return provider_state_request

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
