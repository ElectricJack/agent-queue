from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderOverride")


@_attrs_define
class ProviderOverride:
    """An operator override (D6).  ``until`` is ``None`` only for ``disabled``.

    Attributes:
        state (str):
        until (float | None | Unset):
        by (None | str | Unset):
        reason (None | str | Unset):
        set_at (float | None | Unset):
    """

    state: str
    until: float | None | Unset = UNSET
    by: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    set_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        state = self.state

        until: float | None | Unset
        if isinstance(self.until, Unset):
            until = UNSET
        else:
            until = self.until

        by: None | str | Unset
        if isinstance(self.by, Unset):
            by = UNSET
        else:
            by = self.by

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        set_at: float | None | Unset
        if isinstance(self.set_at, Unset):
            set_at = UNSET
        else:
            set_at = self.set_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "state": state,
            }
        )
        if until is not UNSET:
            field_dict["until"] = until
        if by is not UNSET:
            field_dict["by"] = by
        if reason is not UNSET:
            field_dict["reason"] = reason
        if set_at is not UNSET:
            field_dict["set_at"] = set_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        state = d.pop("state")

        def _parse_until(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        until = _parse_until(d.pop("until", UNSET))

        def _parse_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        by = _parse_by(d.pop("by", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_set_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        set_at = _parse_set_at(d.pop("set_at", UNSET))

        provider_override = cls(
            state=state,
            until=until,
            by=by,
            reason=reason,
            set_at=set_at,
        )

        provider_override.additional_properties = d
        return provider_override

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
