from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationPushChange")


@_attrs_define
class ProviderAllocationPushChange:
    """A manual agent definition whose push eligibility the request changes.

    Attributes:
        agent_id (str):
        profile_id (str):
        provider (str):
        push_before (bool):
        push_after (bool):
        name (None | str | Unset):
        effective_harness (None | str | Unset):
        state (None | str | Unset):
        current_task_id (None | str | Unset):
    """

    agent_id: str
    profile_id: str
    provider: str
    push_before: bool
    push_after: bool
    name: None | str | Unset = UNSET
    effective_harness: None | str | Unset = UNSET
    state: None | str | Unset = UNSET
    current_task_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        agent_id = self.agent_id

        profile_id = self.profile_id

        provider = self.provider

        push_before = self.push_before

        push_after = self.push_after

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        effective_harness: None | str | Unset
        if isinstance(self.effective_harness, Unset):
            effective_harness = UNSET
        else:
            effective_harness = self.effective_harness

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

        current_task_id: None | str | Unset
        if isinstance(self.current_task_id, Unset):
            current_task_id = UNSET
        else:
            current_task_id = self.current_task_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "agent_id": agent_id,
                "profile_id": profile_id,
                "provider": provider,
                "push_before": push_before,
                "push_after": push_after,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if effective_harness is not UNSET:
            field_dict["effective_harness"] = effective_harness
        if state is not UNSET:
            field_dict["state"] = state
        if current_task_id is not UNSET:
            field_dict["current_task_id"] = current_task_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_id = d.pop("agent_id")

        profile_id = d.pop("profile_id")

        provider = d.pop("provider")

        push_before = d.pop("push_before")

        push_after = d.pop("push_after")

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_effective_harness(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        effective_harness = _parse_effective_harness(d.pop("effective_harness", UNSET))

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        def _parse_current_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_task_id = _parse_current_task_id(d.pop("current_task_id", UNSET))

        provider_allocation_push_change = cls(
            agent_id=agent_id,
            profile_id=profile_id,
            provider=provider,
            push_before=push_before,
            push_after=push_after,
            name=name,
            effective_harness=effective_harness,
            state=state,
            current_task_id=current_task_id,
        )

        provider_allocation_push_change.additional_properties = d
        return provider_allocation_push_change

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
