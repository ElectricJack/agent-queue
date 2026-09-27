from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationSession")


@_attrs_define
class ProviderAllocationSession:
    """One live session of an ordinary worker profile.

    Attributes:
        session_id (str):
        lifecycle (str):
        state (str):
        activity (str):
        project_id (None | str | Unset):
        agent_id (None | str | Unset):
        task_id (None | str | Unset):
        task_title (None | str | Unset):
        idle_seconds (float | None | Unset):
        started_at (float | None | Unset):
    """

    session_id: str
    lifecycle: str
    state: str
    activity: str
    project_id: None | str | Unset = UNSET
    agent_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    task_title: None | str | Unset = UNSET
    idle_seconds: float | None | Unset = UNSET
    started_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        session_id = self.session_id

        lifecycle = self.lifecycle

        state = self.state

        activity = self.activity

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        agent_id: None | str | Unset
        if isinstance(self.agent_id, Unset):
            agent_id = UNSET
        else:
            agent_id = self.agent_id

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        task_title: None | str | Unset
        if isinstance(self.task_title, Unset):
            task_title = UNSET
        else:
            task_title = self.task_title

        idle_seconds: float | None | Unset
        if isinstance(self.idle_seconds, Unset):
            idle_seconds = UNSET
        else:
            idle_seconds = self.idle_seconds

        started_at: float | None | Unset
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        else:
            started_at = self.started_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "session_id": session_id,
                "lifecycle": lifecycle,
                "state": state,
                "activity": activity,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if agent_id is not UNSET:
            field_dict["agent_id"] = agent_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if task_title is not UNSET:
            field_dict["task_title"] = task_title
        if idle_seconds is not UNSET:
            field_dict["idle_seconds"] = idle_seconds
        if started_at is not UNSET:
            field_dict["started_at"] = started_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        session_id = d.pop("session_id")

        lifecycle = d.pop("lifecycle")

        state = d.pop("state")

        activity = d.pop("activity")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_agent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        agent_id = _parse_agent_id(d.pop("agent_id", UNSET))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_task_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_title = _parse_task_title(d.pop("task_title", UNSET))

        def _parse_idle_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        idle_seconds = _parse_idle_seconds(d.pop("idle_seconds", UNSET))

        def _parse_started_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))

        provider_allocation_session = cls(
            session_id=session_id,
            lifecycle=lifecycle,
            state=state,
            activity=activity,
            project_id=project_id,
            agent_id=agent_id,
            task_id=task_id,
            task_title=task_title,
            idle_seconds=idle_seconds,
            started_at=started_at,
        )

        provider_allocation_session.additional_properties = d
        return provider_allocation_session

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
