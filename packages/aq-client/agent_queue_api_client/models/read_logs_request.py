from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReadLogsRequest")


@_attrs_define
class ReadLogsRequest:
    """
    Attributes:
        level (str | Unset): Minimum log severity level. Only entries at or above this level are returned. Default:
            'info'.
        since (None | str | Unset): Only return log entries newer than this relative time. Accepts durations like '5m',
            '1h', '2d', '30s'.
        limit (int | Unset): Maximum number of log entries to return (default 100). Default: 100.
        component (None | str | Unset): Filter by component name (e.g. 'orchestrator', 'supervisor', 'api', 'hooks',
            'discord').
        task_id (None | str | Unset): Filter by task ID.
        project_id (None | str | Unset): Filter by project ID.
        pattern (None | str | Unset): Substring search in the log message/event field (case-insensitive).
    """

    level: str | Unset = "info"
    since: None | str | Unset = UNSET
    limit: int | Unset = 100
    component: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    pattern: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        level = self.level

        since: None | str | Unset
        if isinstance(self.since, Unset):
            since = UNSET
        else:
            since = self.since

        limit = self.limit

        component: None | str | Unset
        if isinstance(self.component, Unset):
            component = UNSET
        else:
            component = self.component

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        pattern: None | str | Unset
        if isinstance(self.pattern, Unset):
            pattern = UNSET
        else:
            pattern = self.pattern

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if level is not UNSET:
            field_dict["level"] = level
        if since is not UNSET:
            field_dict["since"] = since
        if limit is not UNSET:
            field_dict["limit"] = limit
        if component is not UNSET:
            field_dict["component"] = component
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if pattern is not UNSET:
            field_dict["pattern"] = pattern

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        level = d.pop("level", UNSET)

        def _parse_since(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        since = _parse_since(d.pop("since", UNSET))

        limit = d.pop("limit", UNSET)

        def _parse_component(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        component = _parse_component(d.pop("component", UNSET))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_pattern(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pattern = _parse_pattern(d.pop("pattern", UNSET))

        read_logs_request = cls(
            level=level,
            since=since,
            limit=limit,
            component=component,
            task_id=task_id,
            project_id=project_id,
            pattern=pattern,
        )

        read_logs_request.additional_properties = d
        return read_logs_request

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
