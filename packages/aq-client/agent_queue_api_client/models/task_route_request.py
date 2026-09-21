from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskRouteRequest")


@_attrs_define
class TaskRouteRequest:
    """
    Attributes:
        task_id (str): Task ID to route
        profile_id (str): Eligible worker profile ID that should execute the task (never supervisor)
        provider_intent (None | str | Unset): Whether anyone meant the provider profile_id names (provider-failover D8).
            Default: preferred when you pass profile_id, else class_only. A preferred or class_only task fails over to the
            same class on another provider when its provider is unavailable; a pinned one holds. pinned/preferred need a
            profile_id; pinning is refused for worker tokens.
        pin (bool | None | Unset): Shorthand for provider_intent=pinned.
        intelligence_class (None | str | Unset): Intelligence class id (e.g. 'fast-low', 'standard-high', 'astra-high')
            from vault/intelligence-classes/. Requires a matching worker at launch. Omit to preserve the task's existing
            class or profile default.
        workspace_id (None | str | Unset): Workspace to prefer for execution (optional)
    """

    task_id: str
    profile_id: str
    provider_intent: None | str | Unset = UNSET
    pin: bool | None | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    workspace_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        profile_id = self.profile_id

        provider_intent: None | str | Unset
        if isinstance(self.provider_intent, Unset):
            provider_intent = UNSET
        else:
            provider_intent = self.provider_intent

        pin: bool | None | Unset
        if isinstance(self.pin, Unset):
            pin = UNSET
        else:
            pin = self.pin

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        workspace_id: None | str | Unset
        if isinstance(self.workspace_id, Unset):
            workspace_id = UNSET
        else:
            workspace_id = self.workspace_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "profile_id": profile_id,
            }
        )
        if provider_intent is not UNSET:
            field_dict["provider_intent"] = provider_intent
        if pin is not UNSET:
            field_dict["pin"] = pin
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if workspace_id is not UNSET:
            field_dict["workspace_id"] = workspace_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        profile_id = d.pop("profile_id")

        def _parse_provider_intent(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        provider_intent = _parse_provider_intent(d.pop("provider_intent", UNSET))

        def _parse_pin(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        pin = _parse_pin(d.pop("pin", UNSET))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        def _parse_workspace_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        workspace_id = _parse_workspace_id(d.pop("workspace_id", UNSET))

        task_route_request = cls(
            task_id=task_id,
            profile_id=profile_id,
            provider_intent=provider_intent,
            pin=pin,
            intelligence_class=intelligence_class,
            workspace_id=workspace_id,
        )

        task_route_request.additional_properties = d
        return task_route_request

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
