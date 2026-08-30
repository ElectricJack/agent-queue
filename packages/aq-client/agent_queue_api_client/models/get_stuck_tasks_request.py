from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GetStuckTasksRequest")


@_attrs_define
class GetStuckTasksRequest:
    """
    Attributes:
        assigned_threshold_seconds (int | Unset): Max seconds a task may stay ASSIGNED before being flagged as stuck.
            Default 1800 (30 minutes). Default: 1800.
        in_progress_threshold_seconds (int | Unset): Max seconds a task may stay IN_PROGRESS before being flagged as
            stuck.  Default 7200 (2 hours). Default: 7200.
        now (float | None | Unset): Reference Unix timestamp (seconds since epoch).  Playbooks should pass the trigger
            event's ``tick_time`` so repeated runs are deterministic.  Defaults to the server's current time.
        project_id (None | str | Unset): Optional project filter.  When omitted, all projects are scanned.
    """

    assigned_threshold_seconds: int | Unset = 1800
    in_progress_threshold_seconds: int | Unset = 7200
    now: float | None | Unset = UNSET
    project_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        assigned_threshold_seconds = self.assigned_threshold_seconds

        in_progress_threshold_seconds = self.in_progress_threshold_seconds

        now: float | None | Unset
        if isinstance(self.now, Unset):
            now = UNSET
        else:
            now = self.now

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if assigned_threshold_seconds is not UNSET:
            field_dict["assigned_threshold_seconds"] = assigned_threshold_seconds
        if in_progress_threshold_seconds is not UNSET:
            field_dict["in_progress_threshold_seconds"] = in_progress_threshold_seconds
        if now is not UNSET:
            field_dict["now"] = now
        if project_id is not UNSET:
            field_dict["project_id"] = project_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        assigned_threshold_seconds = d.pop("assigned_threshold_seconds", UNSET)

        in_progress_threshold_seconds = d.pop("in_progress_threshold_seconds", UNSET)

        def _parse_now(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        now = _parse_now(d.pop("now", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        get_stuck_tasks_request = cls(
            assigned_threshold_seconds=assigned_threshold_seconds,
            in_progress_threshold_seconds=in_progress_threshold_seconds,
            now=now,
            project_id=project_id,
        )

        get_stuck_tasks_request.additional_properties = d
        return get_stuck_tasks_request

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
