from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="FlockSession")


@_attrs_define
class FlockSession:
    """
    Attributes:
        session_id (str):
        name (str):
        role (str):
        scope (str):
        harness (str):
        profile_id (str):
        state (str):
        desired_state (str):
        started_at (float):
        uptime_seconds (float):
        lifecycle (str):
        agent_id (None | str | Unset):
        project_id (None | str | Unset):
        provider (None | str | Unset):
        model (None | str | Unset):
        intelligence_class (None | str | Unset):
        task_id (None | str | Unset):
        last_activity (float | None | Unset):
    """

    session_id: str
    name: str
    role: str
    scope: str
    harness: str
    profile_id: str
    state: str
    desired_state: str
    started_at: float
    uptime_seconds: float
    lifecycle: str
    agent_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    provider: None | str | Unset = UNSET
    model: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    last_activity: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        session_id = self.session_id

        name = self.name

        role = self.role

        scope = self.scope

        harness = self.harness

        profile_id = self.profile_id

        state = self.state

        desired_state = self.desired_state

        started_at = self.started_at

        uptime_seconds = self.uptime_seconds

        lifecycle = self.lifecycle

        agent_id: None | str | Unset
        if isinstance(self.agent_id, Unset):
            agent_id = UNSET
        else:
            agent_id = self.agent_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        provider: None | str | Unset
        if isinstance(self.provider, Unset):
            provider = UNSET
        else:
            provider = self.provider

        model: None | str | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        last_activity: float | None | Unset
        if isinstance(self.last_activity, Unset):
            last_activity = UNSET
        else:
            last_activity = self.last_activity

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "session_id": session_id,
                "name": name,
                "role": role,
                "scope": scope,
                "harness": harness,
                "profile_id": profile_id,
                "state": state,
                "desired_state": desired_state,
                "started_at": started_at,
                "uptime_seconds": uptime_seconds,
                "lifecycle": lifecycle,
            }
        )
        if agent_id is not UNSET:
            field_dict["agent_id"] = agent_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if provider is not UNSET:
            field_dict["provider"] = provider
        if model is not UNSET:
            field_dict["model"] = model
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if last_activity is not UNSET:
            field_dict["last_activity"] = last_activity

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        session_id = d.pop("session_id")

        name = d.pop("name")

        role = d.pop("role")

        scope = d.pop("scope")

        harness = d.pop("harness")

        profile_id = d.pop("profile_id")

        state = d.pop("state")

        desired_state = d.pop("desired_state")

        started_at = d.pop("started_at")

        uptime_seconds = d.pop("uptime_seconds")

        lifecycle = d.pop("lifecycle")

        def _parse_agent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        agent_id = _parse_agent_id(d.pop("agent_id", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        provider = _parse_provider(d.pop("provider", UNSET))

        def _parse_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model = _parse_model(d.pop("model", UNSET))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_last_activity(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        last_activity = _parse_last_activity(d.pop("last_activity", UNSET))

        flock_session = cls(
            session_id=session_id,
            name=name,
            role=role,
            scope=scope,
            harness=harness,
            profile_id=profile_id,
            state=state,
            desired_state=desired_state,
            started_at=started_at,
            uptime_seconds=uptime_seconds,
            lifecycle=lifecycle,
            agent_id=agent_id,
            project_id=project_id,
            provider=provider,
            model=model,
            intelligence_class=intelligence_class,
            task_id=task_id,
            last_activity=last_activity,
        )

        flock_session.additional_properties = d
        return flock_session

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
