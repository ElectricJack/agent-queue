from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderRerouteRequest")


@_attrs_define
class ProviderRerouteRequest:
    """
    Attributes:
        provider (None | str | Unset): Limit the sweep to one provider key or vendor alias.
        task_id (list[Any] | None | Unset): Task id(s) to move explicitly.
        to_profile (None | str | Unset): Target profile for the named tasks.
        include_paused (bool | None | Unset): Also resume and move tasks paused before failover recorded a cause (listed
            first with --dry-run).
        dry_run (bool | None | Unset): Plan only; write nothing.
        force (bool | None | Unset): Operator override for named tasks: move a pinned task, target a degraded provider,
            or change the class with --to-profile.
    """

    provider: None | str | Unset = UNSET
    task_id: list[Any] | None | Unset = UNSET
    to_profile: None | str | Unset = UNSET
    include_paused: bool | None | Unset = UNSET
    dry_run: bool | None | Unset = UNSET
    force: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        provider: None | str | Unset
        if isinstance(self.provider, Unset):
            provider = UNSET
        else:
            provider = self.provider

        task_id: list[Any] | None | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        elif isinstance(self.task_id, list):
            task_id = self.task_id

        else:
            task_id = self.task_id

        to_profile: None | str | Unset
        if isinstance(self.to_profile, Unset):
            to_profile = UNSET
        else:
            to_profile = self.to_profile

        include_paused: bool | None | Unset
        if isinstance(self.include_paused, Unset):
            include_paused = UNSET
        else:
            include_paused = self.include_paused

        dry_run: bool | None | Unset
        if isinstance(self.dry_run, Unset):
            dry_run = UNSET
        else:
            dry_run = self.dry_run

        force: bool | None | Unset
        if isinstance(self.force, Unset):
            force = UNSET
        else:
            force = self.force

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if provider is not UNSET:
            field_dict["provider"] = provider
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if to_profile is not UNSET:
            field_dict["to_profile"] = to_profile
        if include_paused is not UNSET:
            field_dict["include_paused"] = include_paused
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if force is not UNSET:
            field_dict["force"] = force

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        provider = _parse_provider(d.pop("provider", UNSET))

        def _parse_task_id(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                task_id_type_0 = cast(list[Any], data)

                return task_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_to_profile(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        to_profile = _parse_to_profile(d.pop("to_profile", UNSET))

        def _parse_include_paused(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        include_paused = _parse_include_paused(d.pop("include_paused", UNSET))

        def _parse_dry_run(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        dry_run = _parse_dry_run(d.pop("dry_run", UNSET))

        def _parse_force(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        force = _parse_force(d.pop("force", UNSET))

        provider_reroute_request = cls(
            provider=provider,
            task_id=task_id,
            to_profile=to_profile,
            include_paused=include_paused,
            dry_run=dry_run,
            force=force,
        )

        provider_reroute_request.additional_properties = d
        return provider_reroute_request

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
