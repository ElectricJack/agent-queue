from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskRouteClearedRoute")


@_attrs_define
class TaskRouteClearedRoute:
    """The route ``task_route`` cleared (mandatory routing §7).

    Attributes:
        route_source (None | str | Unset):
        profile_id (None | str | Unset):
        intelligence_class (None | str | Unset):
        provider_intent (None | str | Unset):
    """

    route_source: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    provider_intent: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        route_source: None | str | Unset
        if isinstance(self.route_source, Unset):
            route_source = UNSET
        else:
            route_source = self.route_source

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        provider_intent: None | str | Unset
        if isinstance(self.provider_intent, Unset):
            provider_intent = UNSET
        else:
            provider_intent = self.provider_intent

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if route_source is not UNSET:
            field_dict["route_source"] = route_source
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if provider_intent is not UNSET:
            field_dict["provider_intent"] = provider_intent

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_route_source(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        route_source = _parse_route_source(d.pop("route_source", UNSET))

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        def _parse_provider_intent(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        provider_intent = _parse_provider_intent(d.pop("provider_intent", UNSET))

        task_route_cleared_route = cls(
            route_source=route_source,
            profile_id=profile_id,
            intelligence_class=intelligence_class,
            provider_intent=provider_intent,
        )

        task_route_cleared_route.additional_properties = d
        return task_route_cleared_route

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
