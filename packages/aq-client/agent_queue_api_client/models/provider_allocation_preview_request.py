from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_allocation_preview_request_bounds_type_0 import ProviderAllocationPreviewRequestBoundsType0
    from ..models.provider_allocation_preview_request_receive_new_work_type_0 import (
        ProviderAllocationPreviewRequestReceiveNewWorkType0,
    )


T = TypeVar("T", bound="ProviderAllocationPreviewRequest")


@_attrs_define
class ProviderAllocationPreviewRequest:
    """
    Attributes:
        provider (str): The provider: a key (codex) or vendor (openai).
        profile_ids (list[Any] | None | Unset): Narrow to these ordinary worker profiles of the provider; omit to select
            all of them.
        participation (None | str | Unset): The lifecycle every selected profile gets.
        bounds (None | ProviderAllocationPreviewRequestBoundsType0 | Unset): Per selected pool profile: {"min": N,
            "max": N}; "max": null (or "unbounded") removes the ceiling.  Validated as pool_scale validates.
        receive_new_work (None | ProviderAllocationPreviewRequestReceiveNewWorkType0 | Unset): One project's preferred
            provider for unpinned work: {"project_id": "...", "mode": "prefer" | "clear"}.
        drain (None | str | Unset): How displaced sessions stop: graceful (default; busy work finishes), idle-now (idle
            workers terminate now) or interrupt-busy (operator only; busy work is interrupted).
        allow_pinned_wait (bool | None | Unset): Acknowledge pinned READY tasks left on profiles leaving the pool.
    """

    provider: str
    profile_ids: list[Any] | None | Unset = UNSET
    participation: None | str | Unset = UNSET
    bounds: None | ProviderAllocationPreviewRequestBoundsType0 | Unset = UNSET
    receive_new_work: None | ProviderAllocationPreviewRequestReceiveNewWorkType0 | Unset = UNSET
    drain: None | str | Unset = UNSET
    allow_pinned_wait: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.provider_allocation_preview_request_bounds_type_0 import (
            ProviderAllocationPreviewRequestBoundsType0,
        )
        from ..models.provider_allocation_preview_request_receive_new_work_type_0 import (
            ProviderAllocationPreviewRequestReceiveNewWorkType0,
        )

        provider = self.provider

        profile_ids: list[Any] | None | Unset
        if isinstance(self.profile_ids, Unset):
            profile_ids = UNSET
        elif isinstance(self.profile_ids, list):
            profile_ids = self.profile_ids

        else:
            profile_ids = self.profile_ids

        participation: None | str | Unset
        if isinstance(self.participation, Unset):
            participation = UNSET
        else:
            participation = self.participation

        bounds: dict[str, Any] | None | Unset
        if isinstance(self.bounds, Unset):
            bounds = UNSET
        elif isinstance(self.bounds, ProviderAllocationPreviewRequestBoundsType0):
            bounds = self.bounds.to_dict()
        else:
            bounds = self.bounds

        receive_new_work: dict[str, Any] | None | Unset
        if isinstance(self.receive_new_work, Unset):
            receive_new_work = UNSET
        elif isinstance(self.receive_new_work, ProviderAllocationPreviewRequestReceiveNewWorkType0):
            receive_new_work = self.receive_new_work.to_dict()
        else:
            receive_new_work = self.receive_new_work

        drain: None | str | Unset
        if isinstance(self.drain, Unset):
            drain = UNSET
        else:
            drain = self.drain

        allow_pinned_wait: bool | None | Unset
        if isinstance(self.allow_pinned_wait, Unset):
            allow_pinned_wait = UNSET
        else:
            allow_pinned_wait = self.allow_pinned_wait

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
            }
        )
        if profile_ids is not UNSET:
            field_dict["profile_ids"] = profile_ids
        if participation is not UNSET:
            field_dict["participation"] = participation
        if bounds is not UNSET:
            field_dict["bounds"] = bounds
        if receive_new_work is not UNSET:
            field_dict["receive_new_work"] = receive_new_work
        if drain is not UNSET:
            field_dict["drain"] = drain
        if allow_pinned_wait is not UNSET:
            field_dict["allow_pinned_wait"] = allow_pinned_wait

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_allocation_preview_request_bounds_type_0 import (
            ProviderAllocationPreviewRequestBoundsType0,
        )
        from ..models.provider_allocation_preview_request_receive_new_work_type_0 import (
            ProviderAllocationPreviewRequestReceiveNewWorkType0,
        )

        d = dict(src_dict)
        provider = d.pop("provider")

        def _parse_profile_ids(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                profile_ids_type_0 = cast(list[Any], data)

                return profile_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        profile_ids = _parse_profile_ids(d.pop("profile_ids", UNSET))

        def _parse_participation(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        participation = _parse_participation(d.pop("participation", UNSET))

        def _parse_bounds(data: object) -> None | ProviderAllocationPreviewRequestBoundsType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                bounds_type_0 = ProviderAllocationPreviewRequestBoundsType0.from_dict(data)

                return bounds_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderAllocationPreviewRequestBoundsType0 | Unset, data)

        bounds = _parse_bounds(d.pop("bounds", UNSET))

        def _parse_receive_new_work(data: object) -> None | ProviderAllocationPreviewRequestReceiveNewWorkType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                receive_new_work_type_0 = ProviderAllocationPreviewRequestReceiveNewWorkType0.from_dict(data)

                return receive_new_work_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderAllocationPreviewRequestReceiveNewWorkType0 | Unset, data)

        receive_new_work = _parse_receive_new_work(d.pop("receive_new_work", UNSET))

        def _parse_drain(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        drain = _parse_drain(d.pop("drain", UNSET))

        def _parse_allow_pinned_wait(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        allow_pinned_wait = _parse_allow_pinned_wait(d.pop("allow_pinned_wait", UNSET))

        provider_allocation_preview_request = cls(
            provider=provider,
            profile_ids=profile_ids,
            participation=participation,
            bounds=bounds,
            receive_new_work=receive_new_work,
            drain=drain,
            allow_pinned_wait=allow_pinned_wait,
        )

        provider_allocation_preview_request.additional_properties = d
        return provider_allocation_preview_request

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
