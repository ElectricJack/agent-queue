from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskRouteOverrideResponse")


@_attrs_define
class TaskRouteOverrideResponse:
    """An audited emergency override (mandatory routing §7, D2).

    Attributes:
        task_id (str):
        profile_id (str):
        intelligence_class (str):
        by (str):
        success (bool | Unset):  Default: True.
        provider (None | str | Unset):
        provider_intent (str | Unset):  Default: 'pinned'.
        route_source (str | Unset):  Default: 'override'.
        resolved_gate_ids (list[str] | Unset):
    """

    task_id: str
    profile_id: str
    intelligence_class: str
    by: str
    success: bool | Unset = True
    provider: None | str | Unset = UNSET
    provider_intent: str | Unset = "pinned"
    route_source: str | Unset = "override"
    resolved_gate_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        profile_id = self.profile_id

        intelligence_class = self.intelligence_class

        by = self.by

        success = self.success

        provider: None | str | Unset
        if isinstance(self.provider, Unset):
            provider = UNSET
        else:
            provider = self.provider

        provider_intent = self.provider_intent

        route_source = self.route_source

        resolved_gate_ids: list[str] | Unset = UNSET
        if not isinstance(self.resolved_gate_ids, Unset):
            resolved_gate_ids = self.resolved_gate_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "profile_id": profile_id,
                "intelligence_class": intelligence_class,
                "by": by,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if provider is not UNSET:
            field_dict["provider"] = provider
        if provider_intent is not UNSET:
            field_dict["provider_intent"] = provider_intent
        if route_source is not UNSET:
            field_dict["route_source"] = route_source
        if resolved_gate_ids is not UNSET:
            field_dict["resolved_gate_ids"] = resolved_gate_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        profile_id = d.pop("profile_id")

        intelligence_class = d.pop("intelligence_class")

        by = d.pop("by")

        success = d.pop("success", UNSET)

        def _parse_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        provider = _parse_provider(d.pop("provider", UNSET))

        provider_intent = d.pop("provider_intent", UNSET)

        route_source = d.pop("route_source", UNSET)

        resolved_gate_ids = cast(list[str], d.pop("resolved_gate_ids", UNSET))

        task_route_override_response = cls(
            task_id=task_id,
            profile_id=profile_id,
            intelligence_class=intelligence_class,
            by=by,
            success=success,
            provider=provider,
            provider_intent=provider_intent,
            route_source=route_source,
            resolved_gate_ids=resolved_gate_ids,
        )

        task_route_override_response.additional_properties = d
        return task_route_override_response

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
