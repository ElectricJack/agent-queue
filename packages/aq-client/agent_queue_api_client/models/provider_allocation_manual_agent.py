from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationManualAgent")


@_attrs_define
class ProviderAllocationManualAgent:
    """A durable agent definition no live pool session owns.

    ``harness`` / ``intelligence_class`` / ``model`` are the agent's own
    overrides; ``effective_*`` is what a launch would use.  Allocation never
    rewrites any of them.

        Attributes:
            agent_id (str):
            name (str):
            profile_id (str):
            state (str):
            enabled (bool | Unset):  Default: True.
            harness (None | str | Unset):
            intelligence_class (None | str | Unset):
            model (None | str | Unset):
            has_overrides (bool | Unset):  Default: False.
            effective_harness (str | Unset):  Default: ''.
            effective_class (None | str | Unset):
            current_task_id (None | str | Unset):
            current_task_title (None | str | Unset):
            current_project_id (None | str | Unset):
            redacted (bool | Unset):  Default: False.
    """

    agent_id: str
    name: str
    profile_id: str
    state: str
    enabled: bool | Unset = True
    harness: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    model: None | str | Unset = UNSET
    has_overrides: bool | Unset = False
    effective_harness: str | Unset = ""
    effective_class: None | str | Unset = UNSET
    current_task_id: None | str | Unset = UNSET
    current_task_title: None | str | Unset = UNSET
    current_project_id: None | str | Unset = UNSET
    redacted: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        agent_id = self.agent_id

        name = self.name

        profile_id = self.profile_id

        state = self.state

        enabled = self.enabled

        harness: None | str | Unset
        if isinstance(self.harness, Unset):
            harness = UNSET
        else:
            harness = self.harness

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        model: None | str | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        has_overrides = self.has_overrides

        effective_harness = self.effective_harness

        effective_class: None | str | Unset
        if isinstance(self.effective_class, Unset):
            effective_class = UNSET
        else:
            effective_class = self.effective_class

        current_task_id: None | str | Unset
        if isinstance(self.current_task_id, Unset):
            current_task_id = UNSET
        else:
            current_task_id = self.current_task_id

        current_task_title: None | str | Unset
        if isinstance(self.current_task_title, Unset):
            current_task_title = UNSET
        else:
            current_task_title = self.current_task_title

        current_project_id: None | str | Unset
        if isinstance(self.current_project_id, Unset):
            current_project_id = UNSET
        else:
            current_project_id = self.current_project_id

        redacted = self.redacted

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "agent_id": agent_id,
                "name": name,
                "profile_id": profile_id,
                "state": state,
            }
        )
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if harness is not UNSET:
            field_dict["harness"] = harness
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if model is not UNSET:
            field_dict["model"] = model
        if has_overrides is not UNSET:
            field_dict["has_overrides"] = has_overrides
        if effective_harness is not UNSET:
            field_dict["effective_harness"] = effective_harness
        if effective_class is not UNSET:
            field_dict["effective_class"] = effective_class
        if current_task_id is not UNSET:
            field_dict["current_task_id"] = current_task_id
        if current_task_title is not UNSET:
            field_dict["current_task_title"] = current_task_title
        if current_project_id is not UNSET:
            field_dict["current_project_id"] = current_project_id
        if redacted is not UNSET:
            field_dict["redacted"] = redacted

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_id = d.pop("agent_id")

        name = d.pop("name")

        profile_id = d.pop("profile_id")

        state = d.pop("state")

        enabled = d.pop("enabled", UNSET)

        def _parse_harness(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        harness = _parse_harness(d.pop("harness", UNSET))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        def _parse_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model = _parse_model(d.pop("model", UNSET))

        has_overrides = d.pop("has_overrides", UNSET)

        effective_harness = d.pop("effective_harness", UNSET)

        def _parse_effective_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        effective_class = _parse_effective_class(d.pop("effective_class", UNSET))

        def _parse_current_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_task_id = _parse_current_task_id(d.pop("current_task_id", UNSET))

        def _parse_current_task_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_task_title = _parse_current_task_title(d.pop("current_task_title", UNSET))

        def _parse_current_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_project_id = _parse_current_project_id(d.pop("current_project_id", UNSET))

        redacted = d.pop("redacted", UNSET)

        provider_allocation_manual_agent = cls(
            agent_id=agent_id,
            name=name,
            profile_id=profile_id,
            state=state,
            enabled=enabled,
            harness=harness,
            intelligence_class=intelligence_class,
            model=model,
            has_overrides=has_overrides,
            effective_harness=effective_harness,
            effective_class=effective_class,
            current_task_id=current_task_id,
            current_task_title=current_task_title,
            current_project_id=current_project_id,
            redacted=redacted,
        )

        provider_allocation_manual_agent.additional_properties = d
        return provider_allocation_manual_agent

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
