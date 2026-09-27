from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_allocation_bounds_body import ProviderAllocationBoundsBody
    from ..models.provider_allocation_receive_new_work_body import ProviderAllocationReceiveNewWorkBody


T = TypeVar("T", bound="ProviderAllocationPreviewBody")


@_attrs_define
class ProviderAllocationPreviewBody:
    """``POST /api/providers/allocation/preview``: one allocation request (spec §Backend commands).

    Attributes:
        provider (str):
        profile_ids (list[str] | None | Unset):
        participation (None | str | Unset):
        bounds (None | ProviderAllocationBoundsBody | Unset):
        receive_new_work (None | ProviderAllocationReceiveNewWorkBody | Unset):
        drain (None | str | Unset):
        allow_pinned_wait (bool | None | Unset):
    """

    provider: str
    profile_ids: list[str] | None | Unset = UNSET
    participation: None | str | Unset = UNSET
    bounds: None | ProviderAllocationBoundsBody | Unset = UNSET
    receive_new_work: None | ProviderAllocationReceiveNewWorkBody | Unset = UNSET
    drain: None | str | Unset = UNSET
    allow_pinned_wait: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.provider_allocation_bounds_body import ProviderAllocationBoundsBody
        from ..models.provider_allocation_receive_new_work_body import ProviderAllocationReceiveNewWorkBody

        provider = self.provider

        profile_ids: list[str] | None | Unset
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
        elif isinstance(self.bounds, ProviderAllocationBoundsBody):
            bounds = self.bounds.to_dict()
        else:
            bounds = self.bounds

        receive_new_work: dict[str, Any] | None | Unset
        if isinstance(self.receive_new_work, Unset):
            receive_new_work = UNSET
        elif isinstance(self.receive_new_work, ProviderAllocationReceiveNewWorkBody):
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
        from ..models.provider_allocation_bounds_body import ProviderAllocationBoundsBody
        from ..models.provider_allocation_receive_new_work_body import ProviderAllocationReceiveNewWorkBody

        d = dict(src_dict)
        provider = d.pop("provider")

        def _parse_profile_ids(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                profile_ids_type_0 = cast(list[str], data)

                return profile_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        profile_ids = _parse_profile_ids(d.pop("profile_ids", UNSET))

        def _parse_participation(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        participation = _parse_participation(d.pop("participation", UNSET))

        def _parse_bounds(data: object) -> None | ProviderAllocationBoundsBody | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                bounds_type_0 = ProviderAllocationBoundsBody.from_dict(data)

                return bounds_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderAllocationBoundsBody | Unset, data)

        bounds = _parse_bounds(d.pop("bounds", UNSET))

        def _parse_receive_new_work(data: object) -> None | ProviderAllocationReceiveNewWorkBody | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                receive_new_work_type_0 = ProviderAllocationReceiveNewWorkBody.from_dict(data)

                return receive_new_work_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderAllocationReceiveNewWorkBody | Unset, data)

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

        provider_allocation_preview_body = cls(
            provider=provider,
            profile_ids=profile_ids,
            participation=participation,
            bounds=bounds,
            receive_new_work=receive_new_work,
            drain=drain,
            allow_pinned_wait=allow_pinned_wait,
        )

        provider_allocation_preview_body.additional_properties = d
        return provider_allocation_preview_body

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
