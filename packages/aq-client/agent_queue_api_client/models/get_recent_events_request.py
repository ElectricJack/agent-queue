from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GetRecentEventsRequest")


@_attrs_define
class GetRecentEventsRequest:
    """
    Attributes:
        limit (int | Unset): Maximum number of events to return (default 10). Default: 10.
        event_type (None | str | Unset): Filter by event type. Exact match or prefix with '*' (e.g. 'task.*' matches
            task.started, task.completed, etc.).
        since (None | str | Unset): Only return events newer than this relative time. Accepts durations like '5m', '1h',
            '2d', '30s'.
        project_id (None | str | Unset): Filter by project ID.
        agent_id (None | str | Unset): Filter by agent ID.
        task_id (None | str | Unset): Filter by task ID.
    """

    limit: int | Unset = 10
    event_type: None | str | Unset = UNSET
    since: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    agent_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        limit = self.limit

        event_type: None | str | Unset
        if isinstance(self.event_type, Unset):
            event_type = UNSET
        else:
            event_type = self.event_type

        since: None | str | Unset
        if isinstance(self.since, Unset):
            since = UNSET
        else:
            since = self.since

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

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if limit is not UNSET:
            field_dict["limit"] = limit
        if event_type is not UNSET:
            field_dict["event_type"] = event_type
        if since is not UNSET:
            field_dict["since"] = since
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if agent_id is not UNSET:
            field_dict["agent_id"] = agent_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        limit = d.pop("limit", UNSET)

        def _parse_event_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        event_type = _parse_event_type(d.pop("event_type", UNSET))

        def _parse_since(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        since = _parse_since(d.pop("since", UNSET))

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

        get_recent_events_request = cls(
            limit=limit,
            event_type=event_type,
            since=since,
            project_id=project_id,
            agent_id=agent_id,
            task_id=task_id,
        )

        get_recent_events_request.additional_properties = d
        return get_recent_events_request

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
