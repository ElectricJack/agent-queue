from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AssignmentRouteDetail")


@_attrs_define
class AssignmentRouteDetail:
    """
    Attributes:
        source (str):
        intelligence_class (str):
        freshness (str):
        provider (None | str | Unset):
        reason (None | str | Unset):
        playbook_id (None | str | Unset):
        playbook_version (int | None | Unset):
        playbook_run_id (None | str | Unset):
    """

    source: str
    intelligence_class: str
    freshness: str
    provider: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    playbook_id: None | str | Unset = UNSET
    playbook_version: int | None | Unset = UNSET
    playbook_run_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        source = self.source

        intelligence_class = self.intelligence_class

        freshness = self.freshness

        provider: None | str | Unset
        if isinstance(self.provider, Unset):
            provider = UNSET
        else:
            provider = self.provider

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        playbook_id: None | str | Unset
        if isinstance(self.playbook_id, Unset):
            playbook_id = UNSET
        else:
            playbook_id = self.playbook_id

        playbook_version: int | None | Unset
        if isinstance(self.playbook_version, Unset):
            playbook_version = UNSET
        else:
            playbook_version = self.playbook_version

        playbook_run_id: None | str | Unset
        if isinstance(self.playbook_run_id, Unset):
            playbook_run_id = UNSET
        else:
            playbook_run_id = self.playbook_run_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "source": source,
                "intelligence_class": intelligence_class,
                "freshness": freshness,
            }
        )
        if provider is not UNSET:
            field_dict["provider"] = provider
        if reason is not UNSET:
            field_dict["reason"] = reason
        if playbook_id is not UNSET:
            field_dict["playbook_id"] = playbook_id
        if playbook_version is not UNSET:
            field_dict["playbook_version"] = playbook_version
        if playbook_run_id is not UNSET:
            field_dict["playbook_run_id"] = playbook_run_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source = d.pop("source")

        intelligence_class = d.pop("intelligence_class")

        freshness = d.pop("freshness")

        def _parse_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        provider = _parse_provider(d.pop("provider", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_playbook_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        playbook_id = _parse_playbook_id(d.pop("playbook_id", UNSET))

        def _parse_playbook_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        playbook_version = _parse_playbook_version(d.pop("playbook_version", UNSET))

        def _parse_playbook_run_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        playbook_run_id = _parse_playbook_run_id(d.pop("playbook_run_id", UNSET))

        assignment_route_detail = cls(
            source=source,
            intelligence_class=intelligence_class,
            freshness=freshness,
            provider=provider,
            reason=reason,
            playbook_id=playbook_id,
            playbook_version=playbook_version,
            playbook_run_id=playbook_run_id,
        )

        assignment_route_detail.additional_properties = d
        return assignment_route_detail

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
