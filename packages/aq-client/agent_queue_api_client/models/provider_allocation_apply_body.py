from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationApplyBody")


@_attrs_define
class ProviderAllocationApplyBody:
    """``POST /api/providers/allocation/apply``: apply a reviewed preview by its token.

    Attributes:
        preview_token (str):
        authorize_busy_interrupt (list[str] | None | Unset):
        allow_pinned_wait (bool | None | Unset):
    """

    preview_token: str
    authorize_busy_interrupt: list[str] | None | Unset = UNSET
    allow_pinned_wait: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        preview_token = self.preview_token

        authorize_busy_interrupt: list[str] | None | Unset
        if isinstance(self.authorize_busy_interrupt, Unset):
            authorize_busy_interrupt = UNSET
        elif isinstance(self.authorize_busy_interrupt, list):
            authorize_busy_interrupt = self.authorize_busy_interrupt

        else:
            authorize_busy_interrupt = self.authorize_busy_interrupt

        allow_pinned_wait: bool | None | Unset
        if isinstance(self.allow_pinned_wait, Unset):
            allow_pinned_wait = UNSET
        else:
            allow_pinned_wait = self.allow_pinned_wait

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "preview_token": preview_token,
            }
        )
        if authorize_busy_interrupt is not UNSET:
            field_dict["authorize_busy_interrupt"] = authorize_busy_interrupt
        if allow_pinned_wait is not UNSET:
            field_dict["allow_pinned_wait"] = allow_pinned_wait

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        preview_token = d.pop("preview_token")

        def _parse_authorize_busy_interrupt(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                authorize_busy_interrupt_type_0 = cast(list[str], data)

                return authorize_busy_interrupt_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        authorize_busy_interrupt = _parse_authorize_busy_interrupt(d.pop("authorize_busy_interrupt", UNSET))

        def _parse_allow_pinned_wait(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        allow_pinned_wait = _parse_allow_pinned_wait(d.pop("allow_pinned_wait", UNSET))

        provider_allocation_apply_body = cls(
            preview_token=preview_token,
            authorize_busy_interrupt=authorize_busy_interrupt,
            allow_pinned_wait=allow_pinned_wait,
        )

        provider_allocation_apply_body.additional_properties = d
        return provider_allocation_apply_body

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
